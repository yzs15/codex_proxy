# codex-proxy

Codex CLI 的透明重试代理。上游返回容量不足、过载等临时错误时，代理会自动重试，避免错误直接返回给 Codex。

## 安装

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## 配置 `proxy_routes.json`

建议保存为 `~/.codex/proxy_routes.json`：

```json
{
  "upstreams": {
    "ark": {
      "base_url": "https://ark.example.com/v1",
      "api_key": "ark-..."
    },
    "modelserver": {
      "base_url": "https://modelserver.example.com/v1",
      "api_key": "ms-...",
      "strip_reasoning_ids": true
    }
  },
  "default": "modelserver",
  "models": {
    "glm-5.3": "ark",
    "gpt-5.6-sol": "modelserver"
  }
}
```

规则：

- `models` 中列出的模型使用对应上游；
- 未列出的模型使用 `default`；
- `strip_reasoning_ids` 仅给确实不支持 reasoning id 的无状态上游启用。

请保护密钥文件：

```bash
chmod 600 ~/.codex/proxy_routes.json
```

## 启动

```bash
CODEX_PROXY_ROUTES_FILE="$HOME/.codex/proxy_routes.json" \
CODEX_PROXY_BUFFER_FULL_SSE=1 \
CODEX_PROXY_DEBUG=1 \
CODEX_PROXY_DEBUG_LOG=/tmp/codex-proxy-debug.log \
./run.sh
```

代理默认监听 `http://127.0.0.1:8787`。将 Codex 的 provider 地址指向该地址即可。

`CODEX_PROXY_BUFFER_FULL_SSE=1` 会等待整条 SSE 接收完成后再转发，可捕获生成后才出现的容量错误，但会失去实时输出并增加内存占用；不需要时可设为 `0`。

健康检查：

```bash
curl http://127.0.0.1:8787/__proxy_health
```

调试日志：

```bash
tail -f /tmp/codex-proxy-debug.log
```

## 开发

```bash
pip install -e ".[test]"
pytest
```
