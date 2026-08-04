# MangaFinder Webhook

Webhook 用于把已确认的作者动态和每日摘要发送到用户自己的自动化服务。可在“系统设置 → 通知”中配置地址、签名密钥并测试发送。

## 请求

MangaFinder 使用 `POST application/json`，不跟随重定向。接收端返回任意 `2xx` 即表示成功。

```json
{
  "event": "signal_created",
  "idempotency_key": "webhook:signal:42:new_release:confirmed",
  "sent_at": "2026-07-28T12:00:00+00:00",
  "data": {
    "text": "【MangaFinder · 新作预告】\n…"
  }
}
```

当前事件：

- `connection_test`：设置页测试；
- `signal_created`：新作、参展、预售、延期、取消或再版信号；
- `daily_digest`：每日作者动态摘要。

关键请求头：

- `Idempotency-Key`：接收端应持久化去重；
- `X-MangaFinder-Event`：事件名称；
- `X-MangaFinder-Timestamp`：Unix 秒；
- `X-MangaFinder-Signature`：`sha256=<hex digest>`。

## 验签

签名原文是未经修改的 HTTP 请求体字节：

```text
HMAC-SHA256(secret, timestamp + "." + raw_body)
```

接收端应：

1. 拒绝与当前时间相差超过 5 分钟的时间戳；
2. 使用恒定时间比较签名；
3. 验签成功后再解析 JSON；
4. 使用 `Idempotency-Key` 保证重复请求不会重复执行动作。

Python 示例：

```python
import hashlib
import hmac

expected = hmac.new(
    secret.encode(),
    timestamp.encode() + b"." + raw_body,
    hashlib.sha256,
).hexdigest()
valid = hmac.compare_digest(received_signature, f"sha256={expected}")
```

## 重试与安全

失败记录保存在数据库中并指数退避，最长 6 小时。每日摘要会记录已成功渠道，其他渠道失败时不会向成功渠道重复发送。

签名密钥在设置中心加密保存且不会回显，也不会写入通知正文或日志。建议使用 HTTPS、随机长密钥，并让接收端限制请求体大小和处理时长。
