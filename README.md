# astrbot_plugin_quota_checker

查询 AI 中转站（One-API / New-API / Veloera 等）的额度与 Token 消耗统计。

## 使用

私聊或群聊发送：

```
/额度
```

返回示例：

```
📊 账户统计
━━━━━━━━━━━━
📉 总消耗额度：$5.66
🪙 总 Token：1,234,567
━━━━━━━━━━━━
📅 当日消耗额度：$0.09
🪙 当日 Token：1,100
━━━━━━━━━━━━
✅ 剩余额度：$12.34
```

## 配置项

| 配置项 | 类型 | 说明 |
| --- | --- | --- |
| base_url | string | 中转站地址，如 `https://your-newapi.com` |
| api_key | string | 访问令牌（Token） |
| api_path_user | string | 用户信息接口路径，默认 `/api/user/self` |
| api_path_usage | string | Token 用量接口路径，默认 `/api/usage/token` |
| api_path_log | string | 日志接口路径，默认 `/api/log` |
| quota_divisor | int | 额度换算除数，默认 500000；若站点 `/api/status` 返回 `quota_per_unit` 会自动覆盖 |
| api_user_id | string | 中转站用户 ID（New-API 需要，作为 `New-Api-User` 头发送；同时会附带 `Veloera-User`） |
| extra_headers | object | 额外自定义请求头，如 `{"Veloera-User": "1"}` |
| allowed_groups | list | 允许使用的群号列表，留空则所有群可用（私聊不受限制） |

## 说明

- 当日消耗统计依赖 `/api/log` 接口，需要**管理员权限**的令牌；普通用户令牌会自动降级为仅展示账户额度信息
- 不展示总额度
- `/api/status`（无需鉴权）用于自动检测 `quota_per_unit`，减少手动配置
