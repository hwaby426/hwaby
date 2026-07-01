from sqlalchemy import text
from db.database import engine
from loguru import logger


def migrate_add_obv_columns():
    """添加OBV相关字段到daily_kline表"""
    with engine.connect() as conn:
        try:
            conn.execute(text("ALTER TABLE daily_kline ADD COLUMN obv DECIMAL(20,2) COMMENT 'OBV能量潮'"))
            logger.info("已添加 obv 字段")
        except Exception as e:
            if "Duplicate column name" in str(e) or "already exists" in str(e):
                logger.info("obv 字段已存在，跳过")
            else:
                raise

        try:
            conn.execute(text("ALTER TABLE daily_kline ADD COLUMN obv_ma DECIMAL(20,2) COMMENT 'OBV均线'"))
            logger.info("已添加 obv_ma 字段")
        except Exception as e:
            if "Duplicate column name" in str(e) or "already exists" in str(e):
                logger.info("obv_ma 字段已存在，跳过")
            else:
                raise

        conn.commit()
    logger.info("OBV字段迁移完成")


def migrate_add_signal_reason():
    """添加reason字段到trade_signals表"""
    with engine.connect() as conn:
        try:
            conn.execute(text("ALTER TABLE trade_signals ADD COLUMN reason VARCHAR(512) COMMENT '信号触发原因'"))
            logger.info("已添加 reason 字段")
        except Exception as e:
            if "Duplicate column name" in str(e) or "already exists" in str(e):
                logger.info("reason 字段已存在，跳过")
            else:
                raise

        conn.commit()
    logger.info("信号原因字段迁移完成")


def migrate_daily_kline_indexes():
    """重建 daily_kline 表的索引和唯一约束，加入 adjustflag 列"""
    with engine.connect() as conn:
        try:
            conn.execute(text("ALTER TABLE daily_kline DROP INDEX idx_code_date"))
            logger.info("已删除旧索引 idx_code_date")
        except Exception as e:
            if "doesn't exist" in str(e) or "Duplicate key name" in str(e):
                logger.info("idx_code_date 索引不存在，跳过删除")
            else:
                raise

        try:
            conn.execute(text("ALTER TABLE daily_kline DROP INDEX uk_code_date"))
            logger.info("已删除旧唯一约束 uk_code_date")
        except Exception as e:
            if "doesn't exist" in str(e) or "Duplicate key name" in str(e):
                logger.info("uk_code_date 唯一约束不存在，跳过删除")
            else:
                raise

        try:
            conn.execute(text(
                "CREATE UNIQUE INDEX uk_code_adjustflag_date "
                "ON daily_kline (code, adjustflag, trade_date)"
            ))
            logger.info("已添加唯一索引 uk_code_adjustflag_date")
        except Exception as e:
            if "Duplicate key name" in str(e):
                logger.info("uk_code_adjustflag_date 已存在，跳过")
            else:
                raise

        try:
            conn.execute(text(
                "CREATE INDEX idx_code_adjustflag_date "
                "ON daily_kline (code, adjustflag, trade_date)"
            ))
            logger.info("已添加联合索引 idx_code_adjustflag_date")
        except Exception as e:
            if "Duplicate key name" in str(e):
                logger.info("idx_code_adjustflag_date 已存在，跳过")
            else:
                raise

        conn.commit()
    logger.info("daily_kline 索引迁移完成")


def run_all_migrations():
    migrate_add_obv_columns()
    migrate_add_signal_reason()
    migrate_daily_kline_indexes()
    logger.success("所有迁移完成")


if __name__ == '__main__':
    run_all_migrations()
