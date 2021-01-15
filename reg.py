import asyncio
import aiofiles
import json
import configargparse
import contextlib
import logging
import sys
import tkinter as tk
import textwrap

from anyio import create_task_group, run
from dotenv import load_dotenv
from tkinter import messagebox, ttk


class TkAppClosed(Exception):
    def __init__(self, message, type_message='Error'):
        self.message = message
        super().__init__(self.message)
        if message:
            logging.debug(f'{self.message}')
            messagebox.showerror(type_message, self.message)


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


async def update_tk(root_frame):
    while True:
        try:
            root_frame.update()
        except tk.TclError:
            # if application has been destroyed/closed
            raise TkAppClosed('')
        await asyncio.sleep(1)


def process_new_user(input_field, queue):
    text = input_field.get()
    queue.put_nowait(text)
    input_field.delete(0, tk.END)


def draw(queue):
    root = tk.Tk()

    root.title('Регистрация нового пользователя')

    root_frame = tk.Frame()
    root_frame.pack(fill="both", expand=False)

    ttk.Label(root_frame, text="Введите имя пользователя:").pack(side="top")

    input_frame = tk.Frame(root_frame)
    input_frame.pack()

    input_field = tk.Entry(input_frame)
    input_field.pack()
    input_field.bind("<Return>", lambda event: process_new_user(input_field, queue))

    bottom_frame = tk.Frame(root_frame)
    bottom_frame.pack()

    reg_button = ttk.Button(bottom_frame)
    reg_button["text"] = "Регистрация"
    reg_button["command"] = lambda: process_new_user(input_field, queue)
    reg_button.pack(side="left")

    cancel_button = ttk.Button(bottom_frame)
    cancel_button["text"] = "Отмена"
    cancel_button["command"] = lambda: root.destroy()
    cancel_button.pack(side="left")

    return root_frame


async def add_new_user(host, port, queue, path_to_file):
    async with open_socket(host, int(port)) as socket_connection:
        reader, writer = socket_connection
        new_user = await queue.get()
        await reader.readline()
        writer.write(f'\n'.encode())
        await writer.drain()
        await reader.readline()
        writer.write(f'{new_user}\n'.encode())
        await writer.drain()
        chat_message = await reader.readline()
        decoded_chat_message = chat_message.decode()
        if json.loads(decoded_chat_message):
            await save_user_account(json.loads(decoded_chat_message), path_to_file)
            raise TkAppClosed('Новый пользователь успешно зарегистрирован в чате.', 'Info')
        else:
            raise TkAppClosed('Ошибка добавления нового пользователя в чат!', 'Error')


async def save_user_account(user_params, path_to_file):
    async with aiofiles.open(path_to_file, 'w') as file_handler:
        await file_handler.write(
            textwrap.dedent(f'''
            nickname: {user_params["nickname"]}
            account_hash: {user_params["account_hash"]}
            ''')
        )


async def handle_registration(host, port, path_to_file):
    queue = asyncio.Queue()
    root_frame = draw(queue)
    async with create_task_group() as tasks_group:
        await tasks_group.spawn(update_tk, root_frame)
        await tasks_group.spawn(add_new_user, host, port, queue, path_to_file)


def get_args_parser():
    parser = configargparse.ArgParser()
    parser.add_argument('--host', required=False, default='minechat.dvmn.org', help='chat host', env_var='HOST')
    parser.add_argument('--port', required=False, default=5050, help='port', env_var='WRITING_PORT')
    parser.add_argument('--path', required=False, default='user_info.txt', help='path to file for saving account hash')
    return parser


def main():
    load_dotenv()
    logging.basicConfig(level=logging.DEBUG)
    args = get_args_parser().parse_args()
    try:
        run(handle_registration, args.host, args.port, args.path)
    except TkAppClosed as er:
        sys.stderr.write(f'{er.message}\n')
    except KeyboardInterrupt:
        sys.stderr.write("Регистрация прервана.\n")


if __name__ == "__main__":
    main()
