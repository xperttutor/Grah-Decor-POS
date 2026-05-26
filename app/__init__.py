import os
import firebase_admin
from firebase_admin import credentials, firestore
from flask import Flask, request
from flask_login import LoginManager, current_user
from app.config import Config
from app.services.auth_service import User


db = None


def get_db():
    """Get Firestore client instance."""
    global db
    return db


def create_app():
    """Application factory."""
    global db

    app = Flask(__name__)
    app.config.from_object(Config)

    # Initialize Firebase
    # On Cloud Run: set FIREBASE_KEY_PATH="" (or leave it unset) to use
    # Application Default Credentials from the attached service account.
    # Locally: keep serviceAccountKey.json and the default path works as before.
    key_path = app.config['FIREBASE_KEY_PATH']
    if key_path and not os.path.isabs(key_path):
        key_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), key_path)

    if not firebase_admin._apps:
        if key_path and os.path.exists(key_path):
            cred = credentials.Certificate(key_path)
        else:
            # Cloud Run / GCE / Cloud Build — use the attached service account
            cred = credentials.ApplicationDefault()
        firebase_admin.initialize_app(cred, {
            'storageBucket': app.config['STORAGE_BUCKET'],
        })

    db = firestore.client()

    # Initialize Flask-Login
    login_manager = LoginManager(app)
    login_manager.login_view = 'auth.login'

    @login_manager.user_loader
    def load_user(user_id):
        if user_id == app.config.get('LOGIN_USERNAME'):
            return User(id=user_id)
        return None

    @app.before_request
    def require_login():
        if not current_user.is_authenticated:
            if request.endpoint and request.endpoint != 'auth.login' and request.endpoint != 'static':
                return app.login_manager.unauthorized()

    # Register blueprints
    from app.routes.auth import auth_bp
    from app.routes.inventory import inventory_bp
    from app.routes.purchase import purchase_bp
    from app.routes.orders import orders_bp
    from app.routes.cashbook import cashbook_bp
    from app.routes.settlements import settlements_bp
    from app.routes.contact import contact_bp
    from app.routes.snapshots import snapshots_bp
    from app.routes.dashboard import dashboard_bp
    
    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(inventory_bp)
    app.register_blueprint(purchase_bp)
    app.register_blueprint(orders_bp)
    app.register_blueprint(cashbook_bp)
    app.register_blueprint(settlements_bp)
    app.register_blueprint(contact_bp)
    app.register_blueprint(snapshots_bp)

    # Root redirect
    @app.route('/')
    def index():
        from flask import redirect, url_for
        return redirect(url_for('dashboard.dashboard'))

    return app
