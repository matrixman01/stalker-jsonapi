#!/usr/bin/python

import socket
import sys
import time
import struct
import datetime
import argparse
import signal
import threading
import os
import base64
import json
import errno

if sys.version < '3':
    import httplib
    from urlparse import urlparse
    import urllib
else:
    import http.client
    from urllib.parse import urlparse
    import urllib.request, urllib.parse, urllib.error


def main():

    eseq  = 0
    flag  = 0
    f     = 0
    prev_fname = -1
    buf   = ''

    start_segment_time = time.time()
    start_segment_byte = None
    end_segment_byte = 0

    global stop_time

    if args.length:
        stop_time = time.time() + args.start_delay + args.length

    if args.start_delay > 0:
        sys.stderr.write('Start delay %ds\n' % args.start_delay)
        time.sleep(args.start_delay)

    if args.callback_url and args.length:
        async_open_url(args.callback_url, {'action': 'started'})

    sock = get_socket()

    while True:
        try:
            data = bytearray(b" " * 2048)
            size = sock.recv_into(data)
            if data and not buf:
                sys.stderr.write('Start capturing on time: ' + str(time.time()) + "\n")
            buf = data[:size]
            end_segment_byte = end_segment_byte + size
            range_time = time.time() - start_segment_time
        except socket.timeout:
            sock.close()
            buf = ''
            sys.stderr.write('No data, reconnecting...\n')
            sock = get_socket()
            continue
        except socket.error as e:
            if e.errno != errno.EINTR:
                raise
            else:
                continue

        if buf[0] == 71:  # UDP 0x47
            data = buf
        else:  # RTP
            header_size = 12 + 4 * (buf[0] & 16)
            seq = (buf[2] << 8)+buf[3]

            if not flag:
                eseq = seq
                flag = 1
            if eseq != seq:
                sys.stderr.write('RTP: NETWORK CONGESTION - expected %d, received %d\n' % (eseq, seq))
                eseq = seq

            eseq += 1

            if eseq > 65535:
                eseq = 0

            data = buf[header_size:]


        if args.out_file:
            if stop_time and stop_time <= time.time():
                if f:
                    f.close()
                if args.callback_url:
                    async_open_url(args.callback_url, {'action': 'ended'})
                sys.exit()

            if not f:
                f = open(args.out_file, 'ab', args.buffering)

            f.write(data)

        elif args.save_directory:

            fname = datetime.datetime.now().strftime(date_format)

            if prev_fname != fname:
                if f:
                    write_index_file(start_segment_time, prev_fname, start_segment_byte, f.tell(), range_time)
                    start_segment_time = time.time()
                    start_segment_byte = None
                    f.close()
                    async_rm_old_files()


                    if args.callback_url:
                        async_open_url(args.callback_url, {'start_time': int(time.mktime((datetime.datetime.strptime(fname, date_format) - datetime.timedelta(hours=args.pieces_number)).timetuple()))})
                        async_open_url(args.callback_url, {'end_time': int(time.time())})

                f = open(args.save_directory + fname + '.mpg', 'ab', args.buffering)

            prev_fname = fname


            if start_segment_byte == None:
                start_segment_byte = f.tell()

            f.write(data)

            if range_time >= 10:
                write_index_file(start_segment_time, fname, start_segment_byte, f.tell(), range_time)
                start_segment_time = time.time()
                start_segment_byte = None

        else:
            if sys.version < '3':
                sys.stdout.write(data)
            else:
                sys.stdout.buffer.write(data)

def write_index_file(start_segment_time, fname, start_byte, end_byte, duration):
        f = open(args.save_directory + fname + '.idx', 'a')
        out = str(round(start_segment_time)) + ',' + str(round(time.time())) + ',' + str(start_byte) + ',' + str((end_byte - 1)) + ',' + str(round(duration)) + "\n"
        f.write(out)
        f.close()


class AsyncRmOldFiles(threading.Thread):
    def __init__(self):
        super(AsyncRmOldFiles, self).__init__()
        self.from_time = int(time.mktime((datetime.datetime.now() - datetime.timedelta(hours=args.pieces_number)).timetuple()))

    def run(self):
        for file in os.listdir(args.save_directory):
            if file.find('.mpg') == len(file)-4:
                try:
                    if self.from_time > int(time.mktime(datetime.datetime.strptime(file[:-4], date_format).timetuple())):
                        rm_file = args.save_directory + '/' + file
                        sys.stderr.write('Deleting ' + rm_file + '\n')
                        os.remove(rm_file)
                except ValueError:
                    pass
                except OSError as error:
                    sys.stderr.write('Error deleting file ' + file + ' - ' + error.message)
            if file.find('.idx') == len(file)-4:
                try:
                    if self.from_time > int(time.mktime(datetime.datetime.strptime(file[:-4], date_format).timetuple())):
                        rm_file = args.save_directory + '/' + file
                        sys.stderr.write('Deleting index ' + rm_file + '\n')
                        os.remove(rm_file)
                except ValueError:
                    pass
                except OSError as error:
                    sys.stderr.write('Error deleting index file ' + file + ' - ' + error.message)


def async_rm_old_files():
    AsyncRmOldFiles().start()


def signal_handler(signal, frame):
    sys.exit(0)
signal.signal(signal.SIGINT, signal_handler)


def update_end_time(signal, frame):
    async_open_url(args.callback_url, {}, update_stop_time, 'GET')

signal.signal(signal.SIGALRM, update_end_time)


def update_stop_time(response):
    global stop_time
    response = json.loads(response.decode("utf-8"))
    if response['results'] and response['results']['stop']:
        stop_time = response['results']['stop']


class AsyncOpenUrl(threading.Thread):
    def __init__(self, url, params, callback, method):
        super(AsyncOpenUrl, self).__init__()
        self.callback = callback
        self.method = method
        self.url = url
        self.params = params

    def run(self):
        url = urlparse(self.url)
        if sys.version < '3':
            params = urllib.urlencode(self.params)
        else:
            params = urllib.parse.urlencode(self.params)
        sys.stderr.write('Sending ' + params + ' to callback url: ' + self.url + '\n')
        if sys.version < '3':
            conn = httplib.HTTPConnection(url.hostname)
        else:
            conn = http.client.HTTPConnection(url.hostname)
        conn.putrequest(self.method, url.path)
        conn.putheader('Connection', 'close')
        if self.method in ['PUT', 'POST']:
            conn.putheader('Content-Type', 'application/x-www-form-urlencoded')
            conn.putheader('Content-Length', str(len(params)))
        if url.username:
            credentials = '%s:%s' % (url.username, url.password)
            if sys.version > '3':
                credentials = bytearray(credentials.encode("utf-8"))
            auth = base64.b64encode(credentials).decode("utf-8")
            conn.putheader("Authorization", "Basic %s" % auth)
        conn.endheaders()
        if sys.version > '3':
            params = bytearray(params.encode("utf-8"))
        conn.send(params)
        resp = conn.getresponse()
        data = resp.read()
        if sys.version < '3':
            sys.stderr.write(data + '\n')
        else:
            sys.stderr.buffer.write(data)
            sys.stderr.write('\n')
        if self.callback:
            self.callback(data)
        conn.close()


def async_open_url(url, params, callback=None, method='PUT'):
    AsyncOpenUrl(url, params, callback, method).start()


def get_socket():
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((args.ip_address, args.port))
        mreq = struct.pack(">4sl", socket.inet_aton(args.ip_address), socket.INADDR_ANY)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.settimeout(10)
    except socket.error as e:
        sys.stderr.write(e.strerror + ', waiting 30s\n')
        time.sleep(30)
        return get_socket()

    return sock

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Stream dump. Can work with rtp and udp streams.')
    parser.add_argument('-a', '--ip_address', help='ip address', default='224.0.1.2')
    parser.add_argument('-p', '--port', help='port', default=5004, type=int)
    parser.add_argument('-d', '--save_directory', help='directory to save pieces')
    parser.add_argument('-n', '--pieces_number', help='number of pieces', type=int)
    parser.add_argument('-c', '--callback_url', help='callback url, use to send HTTP PUT with start_time/end_time params')
    parser.add_argument('-l', '--length', help='recording length', type=int)
    parser.add_argument('-s', '--start_delay', help='delay before start recording', type=int, default=0)
    parser.add_argument('-o', '--out_file', help='save output to file with specified name')
    parser.add_argument('-b', '--buffering', help='buffer to use when opening files', type=int, default=8)
    args = parser.parse_args()
    args.buffering *= 1024*1024
    stop_time = 0
    #print args
    date_format = '%Y%m%d-%H'
    #date_format = '%Y%m%d-%M'
    sys.stderr.write('Stream dump\n')
    sys.stderr.write('Using %s:%d\n' % (args.ip_address, args.port))
    main()
