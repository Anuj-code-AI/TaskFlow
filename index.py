
import sys
from pathlib import Path

# Get TaskFlow root directory
ROOT_DIR = Path(__file__).resolve().parent

# Add backend directory to Python path
BACKEND_DIR = ROOT_DIR / "backend"
sys.path.insert(0, str(BACKEND_DIR))

# Import FastAPI application
from app.main import app
