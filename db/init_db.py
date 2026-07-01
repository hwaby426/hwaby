from db.database import engine
from db.models import Base
from loguru import logger


def init_db():
    logger.info("开始初始化数据库表...")
    Base.metadata.create_all(bind=engine)
    logger.info("数据库表初始化完成")


if __name__ == '__main__':
    init_db()
