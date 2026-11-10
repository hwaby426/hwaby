import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / '.env')


class Settings:
    DB_HOST = os.getenv('DB_HOST', '127.0.0.1')
    DB_PORT = int(os.getenv('DB_PORT', 3306))
    DB_USER = os.getenv('DB_USER', 'root')
    DB_PASSWORD = os.getenv('DB_PASSWORD', '')
    DB_NAME = os.getenv('DB_NAME', 'stock_analysis')

    @property
    def DATABASE_URL(self):
        return (
            f"mysql+pymysql://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}?charset=utf8mb4"
        )

    # 必须收盘后跑：腾讯 qt.gtimg.cn 只有当前快照，漏一天永久缺失
    DAILY_UPDATE_TIME = os.getenv('DAILY_UPDATE_TIME', '16:30')
    # 旧 5/15min 盯盘遗留参数；1min 盯盘改用 MACD_POLL_INTERVAL
    REALTIME_POLL_INTERVAL = int(os.getenv('REALTIME_POLL_INTERVAL', 3))

    STOCK_POOL = os.getenv('STOCK_POOL', 'sz002624')

    INITIAL_CAPITAL = float(os.getenv('INITIAL_CAPITAL', 100000))
    COMMISSION_RATE = float(os.getenv('COMMISSION_RATE', 0.0003))
    STAMP_DUTY_RATE = float(os.getenv('STAMP_DUTY_RATE', 0.001))
    SLIPPAGE_RATE = float(os.getenv('SLIPPAGE_RATE', 0.001))
    MIN_COMMISSION = 5.0

    LOG_DIR = BASE_DIR / 'logs'
    OUTPUT_DIR = BASE_DIR / 'output'

    # 邮件配置
    SEND_EMAIL = os.getenv('SEND_EMAIL', 'false').lower() == 'true'
    EMAIL_TO = os.getenv('EMAIL_TO', '')
    EMAIL_FROM = os.getenv('EMAIL_FROM', '')
    EMAIL_PASS = os.getenv('EMAIL_PASS', '')
    EMAIL_SMTP_SERVER = os.getenv('EMAIL_SMTP_SERVER', 'smtp.qq.com')
    EMAIL_SMTP_PORT = int(os.getenv('EMAIL_SMTP_PORT', 587))

    @classmethod
    def get_stock_pool(cls):
        raw = cls.STOCK_POOL
        if not raw:
            return []
        return [code.strip() for code in raw.split(',') if code.strip()]


settings = Settings()
