import os
import re
import subprocess
import pycurl
import psutil
import consts
import datetime
import urllib.parse
import utils
from io import BytesIO
from model import database, Download
from playhouse.shortcuts import model_to_dict

class Downloader:

  config = None

  def __init__(self, config):
    self.config = config

  def get_download_info(self, url):

    # try to get more info
    if self.config.is_testing():
      final_url = 'http://192.168.1.2:5000/download/file/2c87720d-5393-124d-a6bf-e78ccd4843f7/default/M2$M1$C1870$34874/01%20Hooked%20On%20A%20Feeling.mp3'
      final_url = 'http://192.168.1.2:5000/download/file/2c87720d-5393-124d-a6bf-e78ccd4843f7/default/M3$Z0$33607/L%27Empereur%20de%20Paris.mkv'
      filename = urllib.parse.unquote(os.path.basename(final_url))
      filesize = 0
    else:
      result = subprocess.run(['plowdown -q --skip-final --printf %d ' + url], shell=True, stdout=subprocess.PIPE, universal_newlines=True)
      if result.returncode != 0 or len(result.stdout) == 0:
        return None

      # final url
      final_url = result.stdout
      filename = urllib.parse.unquote(os.path.basename(final_url))
      filename = urllib.parse.quote(filename, safe='!#,.-_&')
      filesize = 0

    # now try to get filesize
    try:
      c = pycurl.Curl()
      headers = BytesIO()
      c.setopt(c.URL, final_url)
      c.setopt(c.HEADER, 1)
      c.setopt(c.NOBODY, 1) # header only, no body
      c.setopt(c.SSL_VERIFYPEER, 0)
      c.setopt(c.SSL_VERIFYHOST, 0)
      c.setopt(c.HEADERFUNCTION, headers.write)
      c.perform()

      # now try to get filesize
      headers = headers.getvalue().decode('utf-8')
      matches = re.findall(r'Content-Length: ([0-9]*)\r', headers, re.MULTILINE)
      if matches is not None and len(matches) > 0:
        filesize = int(matches[0])
    except:
      print('Error while getting filesize')

    # save this
    download = Download()
    download.url = url
    download.download_url = final_url
    download.filename = filename
    download.filesize = filesize

    # done
    return download

  def download(self, dld):

    # cleanup
    self.__cleanup(dld)

    # do it
    try:
      if self.config.is_testing():
        p = subprocess.Popen(['wget', '-q', dld.download_url, '&'], cwd=self.config.download_path(), preexec_fn=os.setsid)
      else:
        p = subprocess.Popen(['plowdown', '-q', '-o', self.config.download_path(), dld.url, '&'], cwd=self.config.download_path(), preexec_fn=os.setsid)
      dld.pid = p.pid
      dld.status = consts.STATUS_STARTING
      dld.started_at = datetime.datetime.now()
      dld.save()
      return True
    except Exception as ex:
      print('Error while launching download', ex)
      return False

  def get_status(self, dld):

    # default
    status = {
      'id': dld.id,
      'info': model_to_dict(dld),
      'status': 'created',
      'progress': 0,
      'speed': '',
      'time_left': '',
      'eta': '',
    }

    # the full path to the file
    fullpath = self.__get_fullpath(dld)
    elapsed = datetime.datetime.now() - dld.started_at
    elapsed = elapsed.days * 86400 + elapsed.seconds

    # first leave starting status
    if dld.status == consts.STATUS_STARTING:
      if os.path.exists(fullpath):
        dld.status = consts.STATUS_DOWNLOADING
        dld.save()
      else:
        if elapsed > 5:
          status['status'] = 'error'
        else:
          status['status'] = 'starting'

    # then downloading is the more and may update the status
    if dld.status == consts.STATUS_DOWNLOADING:

      # first check file
      if os.path.exists(fullpath):
        statinfo = os.stat(fullpath)
        currsize = statinfo.st_size
        if currsize == dld.filesize:
          dld.status = consts.STATUS_COMPLETED
          dld.save()
        else:

          if dld.filesize > 0:
            status['progress'] = '{0:2.1f}'.format(currsize / dld.filesize * 100)

          # now check processs
          try:
            p = psutil.Process(dld.pid)
            if p.status() in (psutil.STATUS_STOPPED, psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD):
              raise RuntimeError('Invalid process status: ' + p.status())

            # download in progress
            status['status'] = 'downloading'

            # calculate download speed
            if elapsed > 0:

              speed = currsize / elapsed
              status['speed'] = utils.humansize(speed) + '/s'

              # calculate left
              if dld.filesize > 0:
                left_bytes = dld.filesize - currsize;
                left_seconds = left_bytes / speed
                delta = datetime.timedelta(0, left_seconds)
                status['time_left'] = str(delta).split('.')[0]
                status['eta'] = '{0:%Y-%m-%d %H:%M:%S}'.format(datetime.datetime.now() + delta)

          except Exception as ex:
            print('Reporting error during download', dld.filename, ex)
            status['status'] = 'error'

      else:
        print('File does not exist during download', dld.filename)
        status['status'] = 'error'

    # others are simple
    if dld.status == consts.STATUS_ERROR:
      status['status'] = 'error'
    if dld.status == consts.STATUS_COMPLETED:
      status['status'] = 'completed'
      status['progress'] = 100
    if dld.status == consts.STATUS_PROCESSED:
      status['status'] = 'processed'
      status['progress'] = 100
    if dld.status == consts.STATUS_CANCELLED:
      status['status'] = 'cancelled'
      status['progress'] = 0

    # done
    return status

  def finalize(self, dld, dest, title):

    try:
      fullsrc = self.__get_fullpath(dld)
      fulldst = dest + '/' + title + utils.extension(dld.filename)
      os.rename(fullsrc, fulldst)
      dld.status = consts.STATUS_PROCESSED
      dld.save()
      return True
    except Exception as ex:
      print(ex)
      return False

  def cancel(self, dld):

    # cleanup
    self.__cleanup(dld)

    # mark
    dld.status = consts.STATUS_CANCELLED
    dld.save()

    # successful
    return True

  def __get_fullpath(self, dld):
    return self.config.download_path() + '/' + dld.filename

  def __cleanup(self, dld):

    # first kill process
    if dld.pid > 0 and dld.status == consts.STATUS_DOWNLOADING:
      try:
        p = psutil.Process(dld.pid)
        if p is not None:
          for c in p.children():
            c.kill()
          p.kill()
      except:
        print('Could not terminate process')

    # delete file
    fullpath = self.__get_fullpath(dld)
    if os.path.exists(fullpath):
      try:
        os.remove(fullpath)
      except:
        print('Could not delete files')
    else:
        print('No file to cleanup: ', fullpath)

