"""完整 SQL.gz 备份：包含业务日志、审计和防重事实，恢复验证后才淘汰旧文件。

使用现有调度入口和共享备份目录；逐表导出，不修改业务数据。
任何表失败都使整次备份失败，记录仅含错误类型。
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import gzip
import os
from uuid import uuid4
import time
from datetime import datetime, timedelta
from typing import Optional

from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from common.core.config import get_settings
from common.db.retry import with_db_retry
from common.db.session import async_session_maker
from common.models.db_backup_log import DbBackupLog
from common.utils.backup_paths import ensure_backup_root
from common.utils.time_utils import get_beijing_now

# 每批读取的数据行数，避免大表一次性载入内存
_BATCH_SIZE = 1000

class NonTransactionalBackupTable(ValueError):
    """A consistent InnoDB snapshot excludes nontransactional engines."""


class DbBackupTaskService:
    """数据库备份定时任务服务"""

    def __init__(self):
        self.task_name = "数据库备份"
        # 执行锁：避免定时循环与手动触发并发执行多次备份，互相拖慢
        self._lock = asyncio.Lock()

    async def execute(self) -> None:
        """执行一次数据库备份。

        若已有备份在执行中，则跳过本次执行（避免并发重复备份）。
        """
        if self._lock.locked():
            logger.warning(f"【{self.task_name}】已有备份正在执行，跳过本次触发")
            return
        async with self._lock:
            await self._run_backup()

    async def _run_backup(self) -> None:
        """实际执行一次数据库备份。"""
        logger.info(f"【{self.task_name}】开始执行")
        start_time = time.monotonic()

        settings = get_settings()
        database = settings.mysql_database

        backup_root = ensure_backup_root()
        now = get_beijing_now()
        file_name = f"backup_{database}_{now.strftime('%Y%m%d_%H%M%S_%f')}_{uuid4().hex[:8]}.sql.gz"
        file_path = backup_root / file_name

        table_count = 0
        total_rows = 0
        error_messages: list[str] = []

        try:
            async with async_session_maker() as session:
                async with self._snapshot(session):
                    tables = await self._list_tables(session, database)
                    if not tables:
                        logger.warning(f"【{self.task_name}】未查询到任何数据表，跳过备份")
                    else:
                        logger.info(f"【{self.task_name}】共 {len(tables)} 张表待备份")

                    # 以 gzip 文本模式写入，边导出边落盘，降低内存占用
                    with os.fdopen(os.open(file_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as raw, gzip.open(raw, "wt", encoding="utf-8") as fp:
                        self._write_header(fp, database, now)
                        for index, table in enumerate(tables, start=1):
                            try:
                                rows = await self._dump_table(session, fp, table)
                                table_count += 1
                                total_rows += rows
                                logger.info(
                                    f"【{self.task_name}】({index}/{len(tables)}) 表 {table} 完成，{rows} 行"
                                )
                            except Exception as table_exc:  # 单表失败不中断整体
                                msg = f"表 {table} 备份失败: {type(table_exc).__name__}"
                                logger.error(f"【{self.task_name}】{msg}")
                                error_messages.append(msg)
                        self._write_footer(fp)

            file_size = file_path.stat().st_size if file_path.exists() else 0
            duration_ms = int((time.monotonic() - start_time) * 1000)

            # 有任何单表失败则记为 failed，但保留已生成的文件供排查
            status = "failed" if error_messages else "success"
            error_text = "; ".join(error_messages)[:1000] if error_messages else None

            await self._log_result(
                status=status,
                file_name=file_name,
                file_path=str(file_path),
                file_size=file_size,
                table_count=table_count,
                total_rows=total_rows,
                duration_ms=duration_ms,
                error_message=error_text,
            )

            logger.info(
                f"【{self.task_name}】执行完成，状态: {status}, 文件: {file_name}, "
                f"表数: {table_count}, 行数: {total_rows}, "
                f"大小: {file_size} 字节, 耗时: {duration_ms} ms"
            )
        except Exception as exc:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            error_text = type(exc).__name__
            logger.error(f"【{self.task_name}】执行失败: {error_text}")

            # 失败时清理可能残留的不完整文件
            try:
                if file_path.exists():
                    file_path.unlink()
            except Exception:
                pass

            await self._log_result(
                status="failed",
                file_name=None,
                file_path=None,
                file_size=None,
                table_count=table_count,
                total_rows=total_rows,
                duration_ms=duration_ms,
                error_message=error_text,
            )
        finally:
            # 只有最新备份已有恢复验证且校验和匹配，保留策略才执行淘汰。
            await self._cleanup_expired_backups()

    @staticmethod
    @asynccontextmanager
    async def _snapshot(session):
        original_zone = (await session.execute(text('SELECT @@SESSION.time_zone'))).scalar_one()
        try:
            # TIMESTAMP values must have one transport timezone on both servers.
            await session.execute(text("SET SESSION time_zone='+00:00'"))
            await session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ'))
            await session.execute(text('START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY'))
            yield
        finally:
            await session.execute(text('SET SESSION time_zone=:zone'), {'zone': original_zone})

    async def _list_tables(self, session: AsyncSession, database: str) -> list[str]:
        """查询当前数据库下的所有基础表（不含视图）。"""
        stmt = text(
            """
            SELECT TABLE_NAME, ENGINE
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = :db AND TABLE_TYPE = 'BASE TABLE'
            ORDER BY TABLE_NAME
            """
        )
        result = await session.execute(stmt, {"db": database})
        rows = result.all()
        if any(row[1] != 'InnoDB' for row in rows):
            raise NonTransactionalBackupTable()
        return [row[0] for row in rows]

    @staticmethod
    def _write_header(fp, database: str, now: datetime) -> None:
        """写入备份文件头部信息与会话设置。"""
        fp.write(f"-- 数据库备份文件\n")
        fp.write(f"-- 数据库: {database}\n")
        fp.write(f"-- 备份时间(北京时间): {now.strftime('%Y-%m-%d %H:%M:%S')}\n")
        fp.write("-- 说明: 本文件由定时任务自动生成。全部表备份结构与数据，包含业务日志与防重事实\n\n")
        fp.write("SET NAMES utf8mb4;\n")
        fp.write("SET SQL_MODE='NO_AUTO_VALUE_ON_ZERO';\n")
        fp.write("SET TIME_ZONE='+00:00';\n")
        fp.write("SET FOREIGN_KEY_CHECKS=0;\n\n")

    @staticmethod
    def _write_footer(fp) -> None:
        """写入备份文件尾部。"""
        fp.write("\nSET FOREIGN_KEY_CHECKS=1;\n")

    async def _dump_table(self, session: AsyncSession, fp, table: str) -> int:
        """导出单张表的结构与数据，返回导出的数据行数。"""
        # 1. 表结构
        create_stmt = await session.execute(text(f"SHOW CREATE TABLE `{table}`"))
        create_row = create_stmt.first()
        create_sql = create_row[1] if create_row and len(create_row) > 1 else ""

        fp.write(f"\n-- ----------------------------\n")
        fp.write(f"-- 表结构: {table}\n")
        fp.write(f"-- ----------------------------\n")
        fp.write(f"DROP TABLE IF EXISTS `{table}`;\n")
        fp.write(f"{create_sql};\n\n")

        # 2. 表数据（分批查询，避免一次性载入大表；使用 buffered 查询保证 asyncmy 稳定性）
        fp.write(f"-- 表数据: {table}\n")
        columns = await self._get_columns(session, table)
        col_clause = ", ".join(f"`{c}`" for c in columns)

        row_count = 0
        offset = 0
        while True:
            result = await session.execute(
                text(f"SELECT {col_clause} FROM `{table}` LIMIT :limit OFFSET :offset"),
                {"limit": _BATCH_SIZE, "offset": offset},
            )
            rows = result.fetchall()
            if not rows:
                break
            for row in rows:
                values = ", ".join(self._format_value(v) for v in row)
                fp.write(f"INSERT INTO `{table}` ({col_clause}) VALUES ({values});\n")
            row_count += len(rows)
            if len(rows) < _BATCH_SIZE:
                break
            offset += _BATCH_SIZE

        fp.write("\n")
        return row_count

    async def _get_columns(self, session: AsyncSession, table: str) -> list[str]:
        """按表定义顺序获取列名列表。"""
        stmt = text(
            """
            SELECT COLUMN_NAME
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table
              AND (GENERATION_EXPRESSION IS NULL OR GENERATION_EXPRESSION = '')
            ORDER BY ORDINAL_POSITION
            """
        )
        result = await session.execute(stmt, {"table": table})
        return [row[0] for row in result.all()]

    @staticmethod
    def _format_value(value) -> str:
        """将单个字段值格式化为安全的 SQL 字面量。"""
        if value is None:
            return "NULL"
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, (bytes, bytearray)):
            return f"0x{value.hex()}" if value else "''"
        if isinstance(value, datetime):
            value = value.strftime("%Y-%m-%d %H:%M:%S.%f")
        elif isinstance(value, timedelta):
            microseconds = (value.days * 86400 + value.seconds) * 1_000_000 + value.microseconds
            sign = '-' if microseconds < 0 else ''
            seconds, fraction = divmod(abs(microseconds), 1_000_000)
            hours, seconds = divmod(seconds, 3600)
            minutes, seconds = divmod(seconds, 60)
            value = f'{sign}{hours:02}:{minutes:02}:{seconds:02}.{fraction:06}'
        # Hex text preserves NUL, quotes and newlines independently of SQL escape mode.
        encoded = str(value).encode('utf-8').hex()
        return f"CONVERT(0x{encoded} USING utf8mb4)" if encoded else "''"

    @with_db_retry(max_retries=3, initial_delay=1.0)
    async def _log_result(
        self,
        *,
        status: str,
        file_name: Optional[str],
        file_path: Optional[str],
        file_size: Optional[int],
        table_count: Optional[int],
        total_rows: Optional[int],
        duration_ms: Optional[int],
        error_message: Optional[str],
    ) -> None:
        """写入一条数据库备份日志。"""
        try:
            async with async_session_maker() as session:
                log = DbBackupLog(
                    status=status,
                    file_name=file_name,
                    file_path=file_path,
                    file_size=file_size,
                    table_count=table_count,
                    total_rows=total_rows,
                    duration_ms=duration_ms,
                    error_message=error_message,
                )
                session.add(log)
                await session.commit()
        except Exception as exc:
            logger.error("【{}】记录备份日志失败: {}", self.task_name, type(exc).__name__)

    async def _cleanup_expired_backups(self) -> None:
        """新备份通过隔离恢复且校验和一致后，才按日周恢复点淘汰。"""
        from common.services.backup_retention_service import prune_verified_backups
        try:
            async with async_session_maker() as session:
                result = await prune_verified_backups(session, apply=True)
                logger.info("备份保留检查: {}，删除文件 {} 个", result['status'], len(result['removed']))
        except Exception as exc:
            logger.warning("备份保留检查未完成，旧备份继续保留: {}", type(exc).__name__)

        from common.services.audit_retention import archive_audit_history
        try:
            async with async_session_maker() as session:
                result = await archive_audit_history(session)
                logger.info("操作审计归档: {} 条", result['archived'])
        except Exception as exc:
            logger.warning("操作审计归档未完成，原记录继续保留: {}", type(exc).__name__)


# 全局实例
db_backup_task_service = DbBackupTaskService()
