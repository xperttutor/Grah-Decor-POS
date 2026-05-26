import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-fallback-secret'
    FIREBASE_KEY_PATH = os.environ.get('FIREBASE_KEY_PATH') or 'serviceAccountKey.json'
    STORAGE_BUCKET = os.environ.get('STORAGE_BUCKET') or ''
    LOGIN_USERNAME = os.environ.get('LOGIN_USERNAME') or 'admin'
    LOGIN_PASSWORD = os.environ.get('LOGIN_PASSWORD') or 'password'

