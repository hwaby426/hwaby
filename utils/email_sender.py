"""邮件发送 —— 支持纯文本与 HTML 两种正文。

设计要点（都是踩过的坑）：

1. **`is_html=True` 时用 `multipart/alternative`**，同时挂纯文本备选。
   邮件客户端默认显示 HTML，不支持 HTML 的（或用户手动切"纯文本模式"）还能看到
   `text_fallback`，不会收到一封白屏邮件。
2. **端口 465 走 SSL，其余走 STARTTLS**。QQ/163 的 465 端口若用 `SMTP()` + `starttls()`
   会直接卡住或报 SMTPServerDisconnected，这是最常见的配置失败原因。
3. **HTML 正文必须自带样式**。邮箱会剥掉 `<head>` 里的 `<style>`（Gmail 部分保留、
   Outlook 基本不认），所以 `plain_to_html()` 把样式全部写在元素的 `style` 属性上。
4. 中文主题必须用 `Header(subject, 'utf-8')` 编码，否则在部分客户端显示为乱码。
"""

import smtplib
import ssl
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape as _esc

try:                                        # 项目统一用 loguru，走同一条日志通道
    from loguru import logger
except ImportError:                         # 独立调用本模块时的兜底
    import logging
    logger = logging.getLogger(__name__)

# 邮件正文配色：与 premarket 报告一致（深色 + 涨红跌绿）
_BG = '#0d1117'
_PANEL = '#161b22'
_LINE = '#2a313c'
_TXT = '#e6edf3'
_DIM = '#8b949e'
_UP = '#f85149'
_DOWN = '#3fb950'

# 注意：下面两串字体名必须用**单引号**。它们会被塞进 `style="..."` 属性里，
# 用双引号会把属性提前闭合，整条 font-family 声明被浏览器丢弃（静默失效）。
_FONT = "-apple-system,'Segoe UI','Microsoft YaHei',Roboto,Helvetica,Arial,sans-serif"
# 字符表格要能对齐，**整段必须用同一款字体**，且「中文宽度 = 2 × 西文宽度」。
# 实测（canvas measureText，12~16px 各档）：
#   NSimSun → 恒等于 2.000   ← 唯一在所有字号下都严格等比的
#   SimSun  → 2.000/1.857/1.875 抖动（非整数度量）
#   Consolas → 1.819  Cascadia → 1.707  Sarasa(未装)→1.693
# 所以 NSimSun 排第一，且字号用整数 px（12.5px 会让 NSimSun 掉到 1.857）。
_MONO = "NSimSun,SimSun,'Sarasa Mono SC','Cascadia Mono',Consolas,'Courier New',monospace"


def plain_to_html(text: str, title: str = '', subtitle: str = '') -> str:
    """把纯文本正文包成邮件安全的 HTML（深色卡片 + 等宽 `pre`）。

    用途：那些本来就是「用空格对齐的字符表格」的报告（信号扫描、回测统计），
    改 HTML 后**必须保持等宽字体**，否则列会错位。所以这里用 `<pre>` 而不是
    试图把文本解析成 `<table>` —— 后者对格式稍一变动就会崩。

    Args:
        text: 纯文本正文
        title: 卡片标题（一般传邮件主题）
        subtitle: 副标题，可为空
    """
    body = _esc(text)
    head = ''
    if title:
        head += (f'<div style="font-size:17px;font-weight:600;color:{_TXT};'
                 f'margin:0 0 4px;font-family:{_FONT}">{_esc(title)}</div>')
    if subtitle:
        head += (f'<div style="font-size:12.5px;color:{_DIM};margin:0 0 12px;'
                 f'font-family:{_FONT}">{_esc(subtitle)}</div>')

    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:{_BG}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
       bgcolor="{_BG}" style="background:{_BG}"><tr><td align="center" style="padding:20px 12px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
       style="max-width:900px;background:{_PANEL};border:1px solid {_LINE};
              border-radius:8px;padding:18px 20px">
<tr><td>{head}
<pre style="margin:0;white-space:pre-wrap;word-break:break-word;font-family:{_MONO};
            font-size:13px;line-height:1.5;color:{_TXT}">{body}</pre>
</td></tr></table>
<div style="max-width:900px;margin:12px auto 0;font-size:11.5px;color:{_DIM};
            font-family:{_FONT};text-align:center">
  本邮件由规则引擎自动生成，是信息整理与规则化解读，<b>不是投资建议</b>。
</div>
</td></tr></table></body></html>"""


def _open_server(smtp_server: str, smtp_port: int, timeout: int = 20):
    """按端口选择 SSL（465）或明文 + STARTTLS（25/587）。"""
    if int(smtp_port) == 465:
        ctx = ssl.create_default_context()
        return smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=timeout, context=ctx)

    server = smtplib.SMTP(smtp_server, smtp_port, timeout=timeout)
    try:
        server.starttls(context=ssl.create_default_context())
    except smtplib.SMTPException as e:      # 少数内网中继不支持 STARTTLS
        logger.warning(f'STARTTLS 失败，回退明文连接: {e}')
    return server


def send_email(to_email: str, subject: str, content: str, smtp_server: str = 'smtp.qq.com',
               smtp_port: int = 587, sender_email: str = None, sender_password: str = None,
               is_html: bool = False, text_fallback: str = None,
               cc_email: str = None, timeout: int = 20):
    """
    发送邮件到指定邮箱

    Args:
        to_email: 收件人邮箱（多个用逗号分隔）
        subject: 邮件主题
        content: 邮件内容（`is_html=True` 时为 HTML 源码）
        smtp_server: SMTP服务器，默认qq邮箱
        smtp_port: SMTP端口，默认587；465 自动走 SSL
        sender_email: 发件人邮箱
        sender_password: 发件人授权码
        is_html: True = 按 HTML 渲染；False = 纯文本
        text_fallback: `is_html=True` 时的纯文本备选正文（建议总是给，
                       客户端不支持 HTML 或用户切纯文本模式时才有内容可看）
        cc_email: 抄送，可为空
        timeout: 网络超时秒数
    """
    if not sender_email or not sender_password:
        logger.error("发件人邮箱或授权码未配置，无法发送邮件")
        return False

    if not to_email:
        logger.error("收件人邮箱未配置，无法发送邮件")
        return False

    if is_html and not text_fallback:
        logger.warning('HTML 邮件未提供 text_fallback，纯文本模式下正文将为空')

    try:
        if is_html:
            if text_fallback:
                msg = MIMEMultipart('alternative')
                # 顺序有意义：先纯文本、后 HTML，客户端优先选最后一个能渲染的
                msg.attach(MIMEText(text_fallback, 'plain', 'utf-8'))
                msg.attach(MIMEText(content, 'html', 'utf-8'))
            else:
                msg = MIMEText(content, 'html', 'utf-8')
        else:
            msg = MIMEText(content, 'plain', 'utf-8')

        msg['Subject'] = Header(subject, 'utf-8')
        msg['From'] = sender_email
        msg['To'] = to_email
        if cc_email:
            msg['Cc'] = cc_email

        server = _open_server(smtp_server, smtp_port, timeout=timeout)
        try:
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, [x.strip() for x in
                                           f'{to_email},{cc_email or ""}'.split(',') if x.strip()],
                            msg.as_string())
        finally:
            try:
                server.quit()
            except Exception:               # noqa: BLE001  已发送成功，quit 失败不影响结果
                pass

        logger.info(f"邮件发送成功，收件人: {to_email}")
        return True
    except Exception as e:                  # noqa: BLE001
        logger.error(f"邮件发送失败: {e}")
        return False
