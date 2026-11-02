# 守望职业直播监控

本地网页，查看 OWCS / 守望先锋职业选手、教练和官方转播是否开播。

支持 Twitch、CHZZK、SOOP、哔哩哔哩，以及主要在 YouTube 开播的频道。

## 启动

```bat
python server.py
```

或双击 `start.bat`，然后打开 http://127.0.0.1:8787

第一次扫描大约一分钟。之后默认每 45 秒刷新。

## 使用

- 顶部可以搜索、按赛区 / 位置 / 队伍筛选。
- 星标后打开「开播提醒」，收藏的人开播时浏览器会通知。
- 「添加选手」可以补上名册里没有的直播间。
- 表格里的「隐藏」只影响本机。
- 设置里可以填写 Twitch 开发者密钥。留空时使用公开接口；填了密钥后改走官方 Helix API，更稳。

## 名册

`data/players.json` 是有直播间的公开选手快照。设置里的「更新选手名册」会重新拉取。手动添加的人保存在 `data/custom.json`，不会被覆盖。

## 国内服务器代理

在「设置」中勾选「启用服务器代理」，填写 HTTP 代理地址，例如
`http://127.0.0.1:7890`，并选择需要走代理的平台。默认选择 Twitch、
CHZZK、SOOP、YouTube 和名册更新，哔哩哔哩保持直连。Twitch 的 OAuth
令牌请求与直播查询都会走相同的代理。

- 支持通过 HTTP 代理的 CONNECT 隧道访问 HTTPS 网站，不支持 SOCKS 地址；
  如果使用 Clash 等软件，请填写其 HTTP 或 mixed 端口，而非 SOCKS 端口。
- 需要认证时可填写 `http://username:password@proxy-host:7890`；
  用户名或密码中的特殊字符需要 URL 编码。
- 地址是从**服务器**访问的地址。容器内的 `127.0.0.1` 指容器本身，
  不代表宿主机或你正在使用的电脑。
- 保存后后续请求生效，无需重启；已在执行的请求不会被中断。
- 代理不可用时会显示查询失败，不会自动回退直连。
- 代理启用时未选的平台明确直连；所选平台仍遵循系统的 `NO_PROXY`
  绕过规则。关闭此功能后，恢复原先 urllib 的系统/环境代理行为
  （包括 `HTTP_PROXY`、`HTTPS_PROXY`、`NO_PROXY`）。
- 代理地址保存在服务器的 `data/settings.json`，不会通过状态 API 返回，
  也不会被 Git 跟踪。界面留空表示保留原地址；取消勾选即可停用。
  配置文件包含明文凭据，请限制文件权限，勿将设置接口无保护地暴露在公网。
- 代理仅作用于后端状态查询和名册下载，不代理浏览器中的封面图片或直播播放。

也可以在服务器上直接编辑 `data/settings.json`，保留已有设置并添加：

```json
{
  "proxyEnabled": true,
  "proxyUrl": "http://127.0.0.1:7890",
  "proxyPlatforms": ["twitch", "chzzk", "soop", "youtube", "roster"]
}
```

## 测试

```bat
python -m unittest discover -s tests -v
```
