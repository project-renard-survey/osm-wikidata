#!/usr/bin/python3
import better_exceptions  # noqa: F401
from gevent.server import StreamServer
from gevent.queue import Queue
from gevent.event import Event
from gevent import monkey, spawn, sleep
from gevent.lock import Semaphore
monkey.patch_all()

from matcher import overpass, netstring
import json
import os.path

# Priority queue
# We should switch to a priority queue ordered by number of chunks
# if somebody requests a place with 10 chunks they should go to the back
# of the queue
#
# Abort request
# If a user gives up and closes the page do we should remove their request from
# the queue if nobody else has made the same request.
#
# We can tell the page was closed by checking a websocket heartbeat.

class Counter(object):
    def __init__(self, start=0):
        self.semaphore = Semaphore()
        self.value = start

    def add(self, other):
        self.semaphore.acquire()
        print('add', self.value, other)
        self.value += other
        self.semaphore.release()
        print('done')

    def sub(self, other):
        self.semaphore.acquire()
        print('sub', self.value, other)
        self.value -= other
        self.semaphore.release()
        print('done')

    def get_value(self):
        return self.value


chunk_count = Counter()
task_queue = Queue()
# how many chunks ahead of this socket in the queue
chunk_count_sock = {}
sockets = {}

listen_host, port = 'localhost', 6020

# almost there
# should give status update as each chunk is loaded.
# tell client the length of the rate limit pause

def queue_update(msg_type, msg):
    items = sockets.items()
    msg['type'] = msg_type
    for address, send_queue in list(items):
        if address not in sockets:
            continue
        send_queue.put(msg)

def wait_for_slot():
    print('get status')
    status = overpass.get_status()

    if not status['slots']:
        return
    secs = status['slots'][0]
    if secs <= 0:
        return
    queue_update('status', {'wait': secs})
    sleep(secs)

def process_queue():
    while True:
        item = task_queue.get()
        place = item['place']
        address = item['address']
        for num, chunk in enumerate(item['chunks']):
            oql = chunk.get('oql')
            if not oql:
                continue
            filename = 'overpass/' + chunk['filename']
            msg = {
                'num': num,
                'filename': chunk['filename'],
                'place': place,
            }
            if not os.path.exists(filename):
                wait_for_slot()
                queue_update('run_query', msg)
                print('run query')
                r = overpass.run_query(oql)
                print('query complete')
                with open(filename, 'wb') as out:
                    out.write(r.content)
            print(msg)
            chunk_count.sub(1)
            queue_update('chunk', msg)
        print('item complete')
        item['queue'].put(None)

def handle(sock, address):
    print('New connection from %s:%s' % address)
    try:
        msg = json.loads(netstring.read(sock))
    except json.decoder.JSONDecodeError:
        netstring.write(sock, 'invalid JSON')
        sock.close()
        return

    if msg.get('type') == 'ping':
        print('ping')
        netstring.write(sock, json.dumps({'type': 'pong'}))
        sock.close()
        return

    queued_chunks = chunk_count.get_value()
    chunk_count_sock[address] = queued_chunks

    # print(msg)
    send_queue = Queue()
    task_queue.put({
        'place': msg['place'],
        'address': address,
        'chunks': msg['chunks'],
        'queue': send_queue,
    })
    chunk_count.add(len(msg['chunks']))
    msg = {'type': 'connected', 'queued_chunks': queued_chunks}
    netstring.write(sock, json.dumps(msg))
    reply = netstring.read(sock)
    print('reply:', reply)
    assert reply == 'ack'

    sockets[address] = send_queue
    to_send = send_queue.get()
    while to_send:
        try:
            netstring.write(sock, json.dumps(to_send))
            reply = netstring.read(sock)
            print('reply:', reply)
            assert reply == 'ack'

        except BrokenPipeError:
            print('socket closed')
            sock.close()
            del sockets[address]
            break

        to_send = send_queue.get()

    print('request complete')
    to_send = json.dumps({'type': 'done'})
    netstring.write(sock, to_send)
    reply = netstring.read(sock)
    print('reply:', reply)
    assert reply == 'ack'
    sock.close()
    del sockets[address]

def main():
    spawn(process_queue)
    print('listening on port {}'.format(port))
    server = StreamServer((listen_host, port), handle)
    server.serve_forever()


if __name__ == '__main__':
    main()
