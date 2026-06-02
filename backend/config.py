from pathlib import Path
from dotenv import load_dotenv
import os

load_dotenv()

ANTHROPIC_API_KEY: str = os.environ["ANTHROPIC_API_KEY"]
DATA_DIR: Path = Path(os.getenv("DATA_DIR", "data/profiles"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
