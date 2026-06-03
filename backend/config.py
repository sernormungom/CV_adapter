from pathlib import Path
from dotenv import load_dotenv
import os

load_dotenv()

ANTHROPIC_API_KEY: str = os.environ["ANTHROPIC_API_KEY"]
DATA_DIR: Path = Path(os.getenv("DATA_DIR", "data/profiles"))
JOB_STORE_DIR: Path = Path(os.getenv("JOB_STORE_DIR", "data/job_store/jobs"))
APPLICATION_TRACKER_DIR: Path = Path(os.getenv("APPLICATION_TRACKER_DIR", "data/application_tracker"))
TA_CONFIG_DIR: Path = Path(os.getenv("TA_CONFIG_DIR", "data/ta_config"))

DATA_DIR.mkdir(parents=True, exist_ok=True)
JOB_STORE_DIR.mkdir(parents=True, exist_ok=True)
APPLICATION_TRACKER_DIR.mkdir(parents=True, exist_ok=True)
TA_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
