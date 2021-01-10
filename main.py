import asyncio
import aiofiles
import configargparse
import contextvars
import contextlib
import gui
import json
import logging
import socket
import sys
import time

from anyio import create_task_group, run, ExceptionGroup
from async_timeout import timeout
from datetime import datetime
from dotenv import load_dotenv
from functools import partial

WATCHDOG_TIMEOUT = 10
CHECK_CONN_TIMEOUT = 3

logger = logging.getLogger('watchdog_logger')

widgets = contextvars.ContextVar('widgets')
queues = contextvars.ContextVar('queues')
context = contextvars.copy_context()


@contextlib.asynccontextmanager
async def open_socket(host, port):
    writer = None
    try:
        reader, writer = await asyncio.open_connection(host, port)
        yield (reader, writer)
    finally:
        if writer:
            writer.close()
            await writer.wait_closed()


async def autorise(socket_connection, account_hash):
    reader, writer = socket_connection
    await reader.readline()
    writer.write(f'{account_hash}\n'.encode())
    await writer.drain()
    chat_message = await reader.readline()
    decoded_chat_message = json.loads(chat_message.decode())
    if not decoded_chat_message:
        raise gui.InvalidToken('Сервер его не узнал токен. Пройдите процедуру регистрации.')
    await reader.readline()
    return decoded_chat_message["nickname"]


def authorise_and_connect(need_autorise=False):
    def _authorise_and_connect(handle_msgs):
        async def inner(host, port, account_hash, queue):
            status_queue = context[queues]['status_updates_queue']
            status_queue.put_nowait(gui.ReadConnectionStateChanged.INITIATED)
            status_queue.put_nowait(gui.SendingConnectionStateChanged.INITIATED)
            async with open_socket(host, int(port)) as socket_connection:
                if need_autorise:
                    sending_queue = context[queues]['sending_queue']
                    watchdog_queue = context[queues]['watchdog_queue']
                    user_name = await autorise(socket_connection, account_hash)
                    sending_queue.put_nowait(f'Выполнена авторизация. Пользователь {user_name}.')
                    status_queue.put_nowait(gui.NicknameReceived(user_name))
                    status_queue.put_nowait(gui.SendingConnectionStateChanged.ESTABLISHED)
                    watchdog_queue.put_nowait('Authorization done')
                else:
                    status_queue.put_nowait(gui.ReadConnectionStateChanged.ESTABLISHED)
                await handle_msgs(socket_connection, queue)
        return inner
    return _authorise_and_connect


@authorise_and_connect(need_autorise=False)
async def read_msgs(socket_connection, queue):
    reader, _ = socket_connection
    while True:
        chat_message = await reader.readline()
        if not chat_message:
            continue
        decoded_chat_message = chat_message.decode()
        queue.put_nowait(decoded_chat_message.strip('\n'))
        context[queues]['watchdog_queue'].put_nowait('New message in chat')


@authorise_and_connect(need_autorise=True)
async def send_msgs(socket_connection, queue):
    _, writer = socket_connection
    while True:
        msg = await queue.get()
        writer.write(f'{msg}\n\n'.encode())
        await writer.drain()
        context[queues]['watchdog_queue'].put_nowait('Message sent to chat')


@authorise_and_connect(need_autorise=False)
async def check_the_connection(socket_connection, queue):
    reader, writer = socket_connection
    while True:
        async with timeout(CHECK_CONN_TIMEOUT) as time_out:
            try:
                writer.write('\n\n'.encode())
                writer.drain()
                await reader.readline()
                await asyncio.sleep(1)
            finally:
                if time_out.expired:
                    raise ConnectionError


async def save_msgs(path_to_history, queue):
    async with aiofiles.open(path_to_history, 'a') as file_handler:
        while True:
            msg = await queue.get()
            formatted_date = datetime.now().strftime("%d-%m-%Y %H:%M")
            await file_handler.write(f'[{formatted_date}] {msg}\n')


async def watch_for_connection(watchdog_queue):
    while True:
        async with timeout(WATCHDOG_TIMEOUT) as time_out:
            try:
                msg = await watchdog_queue.get()
                logger.debug(f'[{int(datetime.now().timestamp())}] Connection is alive. {msg}')
            finally:
                if time_out.expired:
                    logger.debug(f'[{int(datetime.now().timestamp())}] Timeout is elapsed.')
                    raise ConnectionError


async def handle_connection(params):
    messages_queue = context[queues]['messages_queue']
    sending_queue = context[queues]['sending_queue']
    status_updates_queue = context[queues]['status_updates_queue']
    watchdog_queue = context[queues]['watchdog_queue']
    root_frame, conversation_panel, labels_panel = context[widgets]
    async with create_task_group() as tasks_group:
        await tasks_group.spawn(read_msgs, params['host'], int(params['lport']), params['hash'], messages_queue)
        await tasks_group.spawn(save_msgs, params['history'], messages_queue)
        await tasks_group.spawn(send_msgs, params['host'], int(params['wport']), params['hash'], sending_queue)
        await tasks_group.spawn(check_the_connection, params['host'], int(params['wport']), params['hash'], sending_queue)
        await tasks_group.spawn(watch_for_connection, watchdog_queue)
        await tasks_group.spawn(gui.update_tk, root_frame)
        await tasks_group.spawn(gui.update_conversation_history, conversation_panel, messages_queue)
        await tasks_group.spawn(gui.update_status_panel, labels_panel, status_updates_queue)


def prepare_connection(reconnect_function):
    async def inner(async_function):
        context.run(widgets.set, ())
        context.run(
            queues.set,
            {
                'messages_queue': asyncio.Queue(),
                'sending_queue': asyncio.Queue(),
                'watchdog_queue': asyncio.Queue(),
                'status_updates_queue': asyncio.Queue()
            }
        )
        gui.draw(context)
        await reconnect_function(async_function)
    return inner


@prepare_connection
async def reconnect_endlessly(async_function):
    failed_attempts_to_open_socket = 0
    while True:
        if failed_attempts_to_open_socket > 3:
            time.sleep(10)  # полностью блокируем работу скрипта в ожидании восстановления соединения
        try:
            await async_function()
        except gui.InvalidToken:
            sys.stderr.write('Connection with wrong token.\n')
            break
        except (ConnectionError, ExceptionGroup, socket.gaierror):
            sys.stderr.write("Отсутствует подключение к интернету\n")
            failed_attempts_to_open_socket += 1
            continue
        except gui.TkAppClosed:
            sys.stderr.write("Вы вышли из чата\n")
            break


def get_args_parser():
    parser = configargparse.ArgParser()
    parser.add_argument('--host', required=False, default='minechat.dvmn.org', help='chat host', env_var='HOST')
    parser.add_argument('--lport', required=False, default=5000, help='port', env_var='LISTENING_PORT')
    parser.add_argument('--wport', required=False, default=5050, help='port', env_var='WRITING_PORT')
    parser.add_argument('--history', required=False, default='chat_history.txt', help='path to file with history')
    parser.add_argument('--hash', required=False, help='account_hash', env_var='ACCOUNT_HASH')
    return parser


def main():
    load_dotenv()
    args = get_args_parser().parse_args()
    logging.basicConfig(level=logging.DEBUG)
    logger.setLevel(logging.DEBUG)
    conn_function = partial(handle_connection, vars(args))
    try:
        run(reconnect_endlessly, conn_function)
    except KeyboardInterrupt:
        sys.stderr.write("Чат завершен\n")


if __name__ == "__main__":
    main()
