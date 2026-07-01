import io
import os
import queue
import sys
import threading
import time
import uuid
from datetime import datetime
from typing import Dict, List, Optional

from flask import Flask, Response, jsonify, render_template, request, send_from_directory
from loguru import logger

# ==================== 命令元数据 ====================
# 每个命令定义: name=CLI命令名, title=展示名称, description=说明, category=分类, params=参数列表
# params 参数定义: {name, label, type, default, required, help, options}
# type: text=文本, number=数字, date=日期, select=下拉, flag=开关(bool)

COMMAND_META = {
    # ========= 初始化 =========
    "init": {
        "title": "初始化数据库",
        "description": "初始化数据库表结构 + 全量拉取历史数据（日线 + 分钟线）",
        "category": "初始化",
        "params": [
            {"name": "stock_pool", "label": "股票池", "type": "text", "default": "",
             "help": "逗号分隔，如 sh.600519,sz.000001。留空使用配置池"},
            {"name": "skip_min_kline", "label": "跳过分钟K线", "type": "flag", "default": False,
             "help": "只拉取日线数据，跳过5min/15min"},
        ],
    },
    "init-market": {
        "title": "全市场日线初始化",
        "description": "从 stock_info 表读取股票列表，拉取全市场日线数据",
        "category": "初始化",
        "params": [
            {"name": "max_stocks", "label": "最大股票数", "type": "number", "default": "",
             "help": "测试用，留空表示全部"},
        ],
    },
    "init-db": {
        "title": "仅初始化表结构",
        "description": "只创建数据库表结构，不拉取任何数据",
        "category": "初始化",
        "params": [],
    },
    "update-stock-list": {
        "title": "更新股票列表",
        "description": "更新全A股票列表（过滤ETF、指数、ST）",
        "category": "初始化",
        "params": [
            {"name": "date", "label": "目标日期", "type": "date", "default": "",
             "help": "YYYY-MM-DD，留空=今天（往前找最近交易日）"},
        ],
    },
    "stock-pool": {
        "title": "查看股票池",
        "description": "查看当前数据库中的股票池",
        "category": "初始化",
        "params": [
            {"name": "market", "label": "市场", "type": "select", "default": "",
             "help": "sh=沪市，sz=深市，留空=全部",
             "options": [{"value": "", "label": "全部"}, {"value": "sh", "label": "沪市"}, {"value": "sz", "label": "深市"}]},
            {"name": "limit", "label": "限制数量", "type": "number", "default": "", "help": "留空不限制"},
        ],
    },

    # ========= 数据更新 =========
    "update-daily": {
        "title": "每日更新（配置池）",
        "description": "更新配置池股票的日线数据并重新计算信号",
        "category": "数据更新",
        "params": [
            {"name": "stock_pool", "label": "股票池", "type": "text", "default": "",
             "help": "逗号分隔，留空使用配置池"},
        ],
    },
    "update-market": {
        "title": "全市场每日更新",
        "description": "全市场股票日线数据增量更新（耗时较长，可能几分钟到几十分钟）",
        "category": "数据更新",
        "params": [],
    },

    # ========= 信号扫描 =========
    "scan-market": {
        "title": "全市场信号扫描",
        "description": "盘中实时扫描 / 历史日期回溯扫描。不传 --date 为盘中模式（需有实时行情）",
        "category": "信号扫描",
        "params": [
            {"name": "strategies", "label": "策略", "type": "text", "default": "", "help": "逗号分隔，如 MACD金叉，留空=全部"},
            {"name": "signal_type", "label": "信号类型", "type": "select", "default": "buy",
             "help": "buy=买入信号，sell=卖出信号，all=全部",
             "options": [{"value": "buy", "label": "买入"}, {"value": "sell", "label": "卖出"}, {"value": "all", "label": "全部"}]},
            {"name": "min_price", "label": "最低价", "type": "number", "default": "2.0", "help": "最低价格过滤"},
            {"name": "max_price", "label": "最高价", "type": "number", "default": "200.0", "help": "最高价格过滤"},
            {"name": "no_save", "label": "不保存到数据库", "type": "flag", "default": False, "help": "仅打印，不写入数据库"},
            {"name": "date", "label": "扫描日期", "type": "date", "default": "", "help": "YYYY-MM-DD，留空=盘中实时扫描"},
            {"name": "codes", "label": "指定股票", "type": "text", "default": "", "help": "逗号分隔，如 sh.600519,sz.000001。留空=扫描全市场"},
            {"name": "no_volume", "label": "关闭成交量过滤", "type": "flag", "default": False, "help": "MACD金叉策略专用，默认开启放量过滤"},
        ],
    },
    "scan": {
        "title": "扫描日线买卖信号",
        "description": "从数据库 trade_signals 表中查询历史信号（需先有 scan-market/update-daily 写入过数据）",
        "category": "信号扫描",
        "params": [
            {"name": "date", "label": "日期", "type": "date", "default": "", "help": "YYYY-MM-DD，留空=今天"},
            {"name": "type", "label": "信号类型", "type": "select", "default": "all",
             "options": [{"value": "all", "label": "全部"}, {"value": "buy", "label": "买入"}, {"value": "sell", "label": "卖出"}]},
            {"name": "min_strength", "label": "最小信号强度", "type": "number", "default": "0", "help": "0~100"},
            {"name": "limit", "label": "最大条数", "type": "number", "default": "50", "help": "返回多少条"},
        ],
    },
    "resonance": {
        "title": "多周期共振扫描",
        "description": "日线+分钟线多周期共振信号（需有分钟K线数据）",
        "category": "信号扫描",
        "params": [
            {"name": "date", "label": "日期", "type": "date", "default": "", "help": "YYYY-MM-DD，留空=今天"},
            {"name": "type", "label": "信号类型", "type": "select", "default": "buy",
             "options": [{"value": "buy", "label": "买入"}, {"value": "sell", "label": "卖出"}]},
            {"name": "min_strength", "label": "最小强度", "type": "number", "default": "50", "help": "0~100"},
        ],
    },
    "analyze-macd": {
        "title": "MACD金叉信号收益分析",
        "description": "分析最近 N 天的 MACD 金叉买入信号，统计每只股票后续收益，用于策略验证",
        "category": "信号扫描",
        "params": [
            {"name": "days", "label": "分析天数", "type": "number", "default": "10", "help": "分析最近多少个交易日"},
            {"name": "codes", "label": "指定股票", "type": "text", "default": "", "help": "逗号分隔，留空=扫描全市场"},
            {"name": "all_code", "label": "全市场模式", "type": "flag", "default": False, "help": "覆盖 --codes，扫描全市场所有股票"},
            {"name": "max_print", "label": "表格最大条数", "type": "number", "default": "500", "help": "输出表格最多显示多少条"},
            {"name": "end", "label": "分析截止日期", "type": "date", "default": "", "help": "YYYY-MM-DD，留空=今天"},
            {"name": "no_volume", "label": "关闭成交量过滤", "type": "flag", "default": False},
        ],
    },
    "macd-intraday-pnl-from-db": {
        "title": "MACD信号持有收益",
        "description": "从数据库读取 MACD 买入信号，假设信号日买入持有到 end 日卖出，统计收益分布",
        "category": "信号扫描",
        "params": [
            {"name": "start", "label": "起始日期", "type": "date", "default": "", "help": "YYYY-MM-DD，筛选 signal_time >= 此日期"},
            {"name": "end", "label": "结束日期", "type": "date", "default": "", "help": "YYYY-MM-DD，筛选信号 + 计算卖出价（若无当日K线，使用实时价）"},
            {"name": "codes", "label": "指定股票", "type": "text", "default": "", "help": "逗号分隔，留空=全部"},
        ],
    },

    # ========= 回测 =========
    "backtest": {
        "title": "单只股票回测",
        "description": "指定股票 + 策略 + 日期范围，回测买卖点收益",
        "category": "回测",
        "params": [
            {"name": "code", "label": "股票代码", "type": "text", "default": "sh.600519", "help": "如 sh.600519"},
            {"name": "strategy", "label": "策略", "type": "text", "default": "MACD金叉", "help": "策略名称，如 MACD金叉"},
            {"name": "start", "label": "起始日期", "type": "date", "default": "", "help": "YYYY-MM-DD"},
            {"name": "end", "label": "结束日期", "type": "date", "default": "", "help": "YYYY-MM-DD"},
            {"name": "capital", "label": "初始资金", "type": "number", "default": "100000"},
            {"name": "period", "label": "K线周期", "type": "select", "default": "daily",
             "options": [{"value": "daily", "label": "日线"}, {"value": "5min", "label": "5分钟"}, {"value": "15min", "label": "15分钟"}]},
            {"name": "t0", "label": "T+0模式", "type": "flag", "default": False, "help": "当天买入当天可卖出"},
        ],
    },
    "backtest-portfolio": {
        "title": "多标的组合回测",
        "description": "多只股票用同一策略进行组合回测",
        "category": "回测",
        "params": [
            {"name": "codes", "label": "股票代码列表", "type": "text", "default": "sh.600519,sz.000001", "help": "逗号分隔"},
            {"name": "strategy", "label": "策略", "type": "text", "default": "MACD金叉"},
            {"name": "start", "label": "起始日期", "type": "date", "default": "", "help": "YYYY-MM-DD"},
            {"name": "end", "label": "结束日期", "type": "date", "default": "", "help": "YYYY-MM-DD"},
            {"name": "capital", "label": "初始资金", "type": "number", "default": "100000"},
            {"name": "t0", "label": "T+0模式", "type": "flag", "default": False},
        ],
    },
    "compare": {
        "title": "多策略对比（单/多只）",
        "description": "对一只或多只股票，跑多个策略，横向对比策略表现",
        "category": "回测",
        "params": [
            {"name": "code", "label": "股票代码", "type": "text", "default": "", "help": "逗号分隔可多只，留空=配置池"},
            {"name": "strategies", "label": "策略列表", "type": "text", "default": "", "help": "逗号分隔，留空=全部策略"},
            {"name": "start", "label": "起始日期", "type": "date", "default": "", "help": "YYYY-MM-DD"},
            {"name": "end", "label": "结束日期", "type": "date", "default": "", "help": "YYYY-MM-DD，留空=今天"},
            {"name": "capital", "label": "初始资金", "type": "number", "default": "100000"},
            {"name": "period", "label": "K线周期", "type": "select", "default": "daily",
             "options": [{"value": "daily", "label": "日线"}, {"value": "5min", "label": "5分钟"}, {"value": "15min", "label": "15分钟"}]},
            {"name": "sort_by", "label": "排序指标", "type": "select", "default": "total_return",
             "options": [
                 {"value": "total_return", "label": "总收益"},
                 {"value": "annual_return", "label": "年化收益"},
                 {"value": "sharpe_ratio", "label": "夏普比率"},
                 {"value": "win_rate", "label": "胜率"},
                 {"value": "profit_factor", "label": "盈亏比"},
             ]},
            {"name": "t0", "label": "T+0模式", "type": "flag", "default": False},
        ],
    },
    "compare-all": {
        "title": "多股票×多策略综合对比",
        "description": "对一组股票跑所有策略，给出策略综合推荐（计算量大，可能耗时较长）",
        "category": "回测",
        "params": [
            {"name": "codes", "label": "股票列表", "type": "text", "default": "", "help": "逗号分隔，留空=配置池"},
            {"name": "all_code", "label": "全市场模式", "type": "flag", "default": False, "help": "覆盖 --codes 和配置池，使用全市场所有股票（极慢）"},
            {"name": "strategies", "label": "策略列表", "type": "text", "default": "", "help": "逗号分隔，留空=全部"},
            {"name": "start", "label": "起始日期", "type": "date", "default": "", "help": "YYYY-MM-DD"},
            {"name": "end", "label": "结束日期", "type": "date", "default": "", "help": "YYYY-MM-DD"},
            {"name": "capital", "label": "初始资金", "type": "number", "default": "100000"},
            {"name": "period", "label": "K线周期", "type": "select", "default": "daily",
             "options": [{"value": "daily", "label": "日线"}, {"value": "5min", "label": "5分钟"}, {"value": "15min", "label": "15分钟"}]},
            {"name": "top_n", "label": "推荐Top N", "type": "number", "default": "3"},
            {"name": "max_stocks", "label": "最多处理数", "type": "number", "default": "", "help": "测试用，留空=全部"},
            {"name": "t0", "label": "T+0模式", "type": "flag", "default": False},
            {"name": "no_volume", "label": "关闭成交量过滤", "type": "flag", "default": False},
        ],
    },

    # ========= 运行时 =========
    "realtime": {
        "title": "实时盯盘",
        "description": "启动实时盯盘监控（长驻运行，需手动停止）",
        "category": "运行时",
        "long_running": True,
        "params": [],
    },
    "scheduler": {
        "title": "完整调度器",
        "description": "启动完整调度器（每日自动更新 + 盘中信号扫描 + 实时盯盘）",
        "category": "运行时",
        "long_running": True,
        "params": [],
    },

    # ========= 辅助工具 =========
    "strategies": {
        "title": "查看可用策略",
        "description": "列出当前系统注册的所有可用策略",
        "category": "辅助工具",
        "params": [],
    },
    "fetch-min-kline": {
        "title": "手动获取分钟K线",
        "description": "获取指定股票和周期的分钟K线数据并保存到数据库",
        "category": "辅助工具",
        "params": [
            {"name": "code", "label": "股票代码", "type": "text", "default": "sh.600519", "required": True},
            {"name": "period", "label": "K线周期", "type": "select", "default": "5min",
             "options": [{"value": "5min", "label": "5分钟"}, {"value": "15min", "label": "15分钟"},
                         {"value": "30min", "label": "30分钟"}, {"value": "60min", "label": "60分钟"}]},
            {"name": "datalen", "label": "数据条数", "type": "number", "default": "1023", "help": "最大1023条"},
        ],
    },
}

# ==================== 日志捕获 ====================

class StreamLogCapture:
    """捕获命令输出，转发到 SSE 流"""

    def __init__(self):
        self.message_queue: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self._capture_level = 0

    def write(self, text):
        if text and text.strip():
            self.message_queue.put(("log", text.rstrip('\n')))

    def flush(self):
        pass

    def put_info(self, text):
        self.message_queue.put(("info", text))

    def put_done(self, text=""):
        self.message_queue.put(("done", text))

    def put_error(self, text):
        self.message_queue.put(("error", text))

    def get_messages(self):
        while not self._stop_event.is_set() or not self.message_queue.empty():
            try:
                msg_type, msg_text = self.message_queue.get(timeout=0.5)
                yield msg_type, msg_text
            except queue.Empty:
                continue

    def stop(self):
        self._stop_event.set()


# 全局任务注册表（方便同时运行多个命令，task_id 区分）
running_tasks: Dict[str, StreamLogCapture] = {}


# ==================== Flask 应用 ====================

app = Flask(__name__)
app.config['JSON_AS_ASCII'] = False


@app.route('/')
def index():
    """主页面"""
    # 按分类分组
    categories: Dict[str, List[dict]] = {}
    for cmd_name, meta in COMMAND_META.items():
        cat = meta["category"]
        if cat not in categories:
            categories[cat] = []
        categories[cat].append({
            "name": cmd_name,
            "title": meta["title"],
            "description": meta["description"],
            "long_running": meta.get("long_running", False),
            "param_count": len(meta["params"]),
        })
    return render_template('index.html', categories=categories, command_meta=COMMAND_META)


@app.route('/api/commands')
def list_commands():
    """返回所有命令元数据（供前端动态渲染）"""
    return jsonify(COMMAND_META)


@app.route('/api/command/<cmd_name>')
def get_command_meta(cmd_name):
    """返回单个命令的元数据"""
    if cmd_name not in COMMAND_META:
        return jsonify({"error": f"未知命令: {cmd_name}"}), 404
    return jsonify(COMMAND_META[cmd_name])


@app.route('/api/run', methods=['POST'])
def run_command():
    """运行命令，返回 task_id，通过 /api/stream/<task_id> 获取流式输出"""
    data = request.get_json(force=True)
    cmd_name = data.get("command")
    params = data.get("params", {})

    if cmd_name not in COMMAND_META:
        return jsonify({"error": f"未知命令: {cmd_name}"}), 404

    task_id = str(uuid.uuid4())[:8]
    capture = StreamLogCapture()
    running_tasks[task_id] = capture

    t = threading.Thread(target=_execute_command, args=(task_id, cmd_name, params, capture), daemon=True)
    t.start()

    return jsonify({"task_id": task_id, "command": cmd_name})


# 子进程注册表（支持取消时杀掉）
running_processes: Dict[str, "subprocess.Popen"] = {}


# 参数名向后兼容映射（旧名 → 新名），避免浏览器缓存旧 HTML 导致参数丢失
PARAM_NAME_MAP = {
    "scan_date": "date",      # scan-market
    "signal_type": "type",    # scan, resonance
    "end_date": "end",        # analyze-macd
}

# ANSI 颜色控制符正则
_ANSI_RE = None

def _strip_ansi(text: str) -> str:
    """去除 ANSI 颜色/样式控制序列"""
    global _ANSI_RE
    import re as _re
    if _ANSI_RE is None:
        _ANSI_RE = _re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
    return _ANSI_RE.sub("", text)

def _normalize_params(params: dict) -> dict:
    """合并新旧参数名，向后兼容浏览器缓存的旧 HTML"""
    normalized = dict(params)
    for old_name, new_name in PARAM_NAME_MAP.items():
        if old_name in normalized and new_name not in normalized:
            normalized[new_name] = normalized[old_name]
    return normalized


def _execute_command(task_id: str, cmd_name: str, params: dict, capture: StreamLogCapture):
    """在子线程中用 subprocess 执行命令，捕获 stdout/stderr"""
    import subprocess

    try:
        capture.put_info(f"▶ 开始执行: {cmd_name} (task={task_id})")
        capture.put_info(f"  参数: {params}")

        # 参数名向后兼容
        params = _normalize_params(params)

        # 从 COMMAND_META 生成命令行参数
        meta = COMMAND_META[cmd_name]
        click_args = [cmd_name]
        for p in meta["params"]:
            pname = p["name"]
            ptype = p["type"]
            val = params.get(pname, p.get("default", ""))

            if ptype == "flag":
                if val or str(val).lower() in ("true", "1", "yes", "on"):
                    click_args.append("--" + pname.replace("_", "-"))
            else:
                if val is not None and str(val).strip() != "":
                    click_args.append("--" + pname.replace("_", "-"))
                    click_args.append(str(val))

        # 完整命令
        full_cmd = [sys.executable, "main.py"] + click_args
        capture.put_info(f"  等效命令: python main.py {' '.join(click_args)}")
        capture.put_info("-" * 60)

        # 强制子进程用 UTF-8 输出，避免 Windows 控制台编码乱码
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

        # 启动子进程（按字节读取，解码时自动检测 UTF-8/GBK/CP936）
        process = subprocess.Popen(
            full_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            cwd=os.path.dirname(os.path.abspath("main.py")) or ".",
            env=env,
        )
        running_processes[task_id] = process

        # 实时逐行读取输出，自动处理编码，去除 ANSI 颜色代码
        buffer = b""
        while True:
            chunk = process.stdout.read(4096)
            if not chunk:
                break
            buffer += chunk

            while b"\n" in buffer or b"\r" in buffer:
                # 找到第一个换行符
                nl_idx = buffer.find(b"\n")
                cr_idx = buffer.find(b"\r")
                if nl_idx == -1:
                    idx = cr_idx
                elif cr_idx == -1:
                    idx = nl_idx
                else:
                    idx = min(nl_idx, cr_idx)

                raw_line = buffer[:idx]
                buffer = buffer[idx + 1:]

                if raw_line:
                    # 尝试 UTF-8 → GBK → CP936 解码
                    for enc in ("utf-8", "gbk", "cp936"):
                        try:
                            line = raw_line.decode(enc)
                            break
                        except (UnicodeDecodeError, LookupError):
                            continue
                    else:
                        line = raw_line.decode("utf-8", errors="replace")

                    line = _strip_ansi(line).strip()
                    if line:
                        capture.write(line)

        # 处理 buffer 中剩余内容
        if buffer.strip():
            for enc in ("utf-8", "gbk", "cp936"):
                try:
                    line = buffer.decode(enc)
                    break
                except (UnicodeDecodeError, LookupError):
                    continue
            else:
                line = buffer.decode("utf-8", errors="replace")
            line = _strip_ansi(line).strip()
            if line:
                capture.write(line)

        return_code = process.wait()
        capture.put_info("-" * 60)

        if return_code == 0:
            capture.put_done("✓ 执行完成 (退出码 0)")
        else:
            capture.put_error(f"✗ 执行失败 (退出码 {return_code})")

    except Exception as e:
        capture.put_error(f"✗ 系统错误: {e}")
        import traceback
        capture.put_error(traceback.format_exc())
    finally:
        running_processes.pop(task_id, None)
        capture.put_done()


@app.route('/api/stream/<task_id>')
def stream_output(task_id):
    """流式输出命令日志（SSE）"""
    capture = running_tasks.get(task_id)
    if capture is None:
        return jsonify({"error": f"任务不存在: {task_id}"}), 404

    def generate():
        yield f"event: info\ndata: 连接已建立，task_id={task_id}\n\n"
        for msg_type, msg_text in capture.get_messages():
            # 转义换行符
            safe_text = msg_text.replace("\n", "\\n").replace("\r", "")
            yield f"event: {msg_type}\ndata: {safe_text}\n\n"
        # 清理
        running_tasks.pop(task_id, None)

    return Response(generate(), mimetype='text/event-stream; charset=utf-8')


@app.route('/api/cancel/<task_id>', methods=['POST'])
def cancel_task(task_id):
    """取消任务：杀掉子进程 + 停止输出流"""
    # 先杀子进程
    proc = running_processes.pop(task_id, None)
    if proc:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()
        except Exception:
            pass

    # 再关闭日志流
    capture = running_tasks.get(task_id)
    if capture:
        capture.stop()
        return jsonify({"status": "stopped", "task_id": task_id})
    return jsonify({"status": "not_found", "task_id": task_id}), 404


@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(app.root_path, 'static'),
                               'favicon.ico', mimetype='image/vnd.microsoft.icon')


if __name__ == '__main__':
    logger.info("=" * 60)
    logger.info("Web 界面启动：http://127.0.0.1:5000")
    logger.info(f"已注册命令: {len(COMMAND_META)} 个")
    for cat in sorted(set(m["category"] for m in COMMAND_META.values())):
        cmds = [k for k, v in COMMAND_META.items() if v["category"] == cat]
        logger.info(f"  [{cat}] {len(cmds)} 个: {', '.join(cmds)}")
    logger.info("=" * 60)
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
