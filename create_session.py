import instaloader
import os
from dotenv import load_dotenv

load_dotenv()

username = os.getenv("INSTAGRAM_USERNAME")
password = os.getenv("INSTAGRAM_PASSWORD")

L = instaloader.Instaloader()
L.login(username, password)
L.save_session_to_file(f'session-{username}')
print('OK! Session saqlandi!')
