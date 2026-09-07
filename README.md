# English Claw · GitHub Actions 版

每日工作日自动推送 5 个英文单词到 3 个钉钉群，完全免费运行在 GitHub Actions 上。

## 推送时刻（北京时间）

| 群名 | 时刻 |
|------|------|
| LSBG-OBD海外业务部 | 09:00 |
| 海外英语能力提升 | 11:30 |
| 海外设计 | 17:30 |

## 时区说明

GitHub Actions cron 使用 UTC 时间。北京时间 = UTC+8。
- 北京 09:00 → UTC 01:00 → `0 1 * * *`
- 北京 11:30 → UTC 03:30 → `30 3 * * *`
- 北京 17:30 → UTC 09:30 → `30 9 * * *`

代码内部统一用北京时间判断工作日和推送时刻。

## 工作日逻辑

- 跳过周末和法定节假日
- 调休上班日照常发
- 节假日数据在 `holidays.json`，每年国务院公布次年安排后更新

## 部署步骤

1. 在 GitHub 创建一个仓库（公开或私有均可）
2. 将本目录所有文件推送到仓库
3. 在仓库 Settings → Secrets and variables → Actions 添加 Secret：
   - `TARGETS`：JSON 数组，格式见下方
4. 在 Actions 页面手动 Run workflow 测试

### TARGETS 格式

```json
[
  {"name":"群名1","webhook":"https://oapi.dingtalk.com/robot/send?access_token=xxx","secret":"SECxxx","hour":9,"minute":0},
  {"name":"群名2","webhook":"...","secret":"...","hour":11,"minute":30},
  {"name":"群名3","webhook":"...","secret":"...","hour":17,"minute":30}
]
```

`hour`/`minute` 是**北京时间**。

## 手动测试

在 GitHub Actions 页面 → English Claw Daily Words → Run workflow：
- `force_send`：勾选则忽略工作日和时间窗口强制发送
- `force_target`：填群名则只发该群

## 本地测试

```bash
export TARGETS='[{"name":"测试群","webhook":"...","secret":"...","hour":9,"minute":0}]'
export FORCE_SEND=1
python index.py
```
