# Discord Voice Watcher Bot

Bot em Python que fica conectado ao Gateway do Discord, escuta eventos de voz e envia um webhook HTTP (`POST`) quando qualquer usuario entra em um canal de voz monitorado.

## 1) Visao geral

- Conecta no Gateway com `discord.py` 2.x
- Usa `on_voice_state_update` para detectar mudancas de estado de voz
- Dispara `POST` para `WEBHOOK_URL` apenas quando:
  - `old_state.channel_id != VOICE_CHANNEL_ID`
  - `new_state.channel_id == VOICE_CHANNEL_ID`
- Inclui header opcional `X-Discord-Webhook-Secret`
- Implementa retry simples em caso de falha no endpoint externo

## 2) Como criar o bot no Discord Developer Portal

1. Acesse [Discord Developer Portal](https://discord.com/developers/applications).
2. Crie uma nova aplicacao.
3. Va em **Bot** e clique em **Add Bot**.
4. Copie o token do bot para usar em `DISCORD_BOT_TOKEN`.
5. Gere o link de convite em **OAuth2 > URL Generator** com:
   - Scope: `bot`
   - Permissoes minimas: `View Channels`
6. Convide o bot para o servidor onde o canal de voz monitorado existe.

## 3) Permissoes e intents necessarias

- Gateway intents usadas no codigo:
  - `guilds`
  - `voice_states`
- Permissoes no servidor:
  - `View Channels`
- O bot nao precisa entrar no canal de voz para monitorar eventos de entrada/saida.

## 4) Como configurar `.env`

Copie o arquivo de exemplo e ajuste os valores:

```bash
cp .env.example .env
```

Variaveis obrigatorias:

- `DISCORD_BOT_TOKEN`
- `VOICE_CHANNEL_ID`
- `WEBHOOK_URL`

Variavel opcional:

- `WEBHOOK_SECRET`

## 5) Como rodar localmente

Requisitos:
- Python 3.10+

Passos:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

Se o bot conectar corretamente, voce vera logs indicando conexao ao Gateway.

## 5.1) Como rodar com Docker

1. Configure seu `.env` (a partir do `.env.example`).
2. Suba com Docker Compose:

```bash
docker compose --env-file .env up -d --build
```

3. Acompanhe os logs:

```bash
docker compose logs -f
```

4. Para parar:

```bash
docker compose down
```

## 6) Observacoes sobre limitacoes da API (texto x voz)

- Presenca e estado de voz sao recebidos via **Gateway events**, nao via REST.
- Este projeto monitora apenas eventos de **canal de voz** (`on_voice_state_update`).
- Nao existe conceito equivalente de "usuario conectado em canal de texto".
- Alteracoes de mute/deaf nao disparam webhook, porque o filtro exige entrada real no canal monitorado.

## Estrutura do projeto

```text
discord-voice-watcher-bot/
├── bot/
│   ├── __init__.py
│   ├── config.py
│   ├── client.py
│   ├── voice_listener.py
│   ├── webhook.py
│   └── logger.py
├── .env.example
├── requirements.txt
├── main.py
└── README.md
```

## Payload enviado para o webhook

```json
{
  "event": "USER_JOINED_MONITORED_VOICE_CHANNEL",
  "occurred_at": "ISO-8601",
  "guild": {
    "id": "string",
    "name": "string"
  },
  "channel": {
    "id": "string",
    "name": "string"
  },
  "user": {
    "id": "string",
    "username": "string",
    "tag": "string"
  }
}
```

