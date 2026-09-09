# Discord Voice Watcher Bot

Bot em Python que fica conectado ao Gateway do Discord, escuta eventos de voz e mensagens de texto, e envia webhooks HTTP (`POST`) para integracao externa.

## 1) Visao geral

- Conecta no Gateway com `discord.py` 2.x
- Usa `on_voice_state_update` para detectar mudancas de estado de voz
- Dispara `POST` para `WEBHOOK_URL` apenas quando:
  - `old_state.channel_id != VOICE_CHANNEL_ID`
  - `new_state.channel_id == VOICE_CHANNEL_ID`
- Usa `on_message` para detectar o comando `!julgar` em canal de texto especifico
- Quando `!julgar` e enviado em `JULGAR_CHANNEL_ID`, busca os primeiros 5 usuarios do servidor
- Envia uma mensagem no canal com opcoes numeradas para o autor escolher
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
7. Em **Bot > Privileged Gateway Intents**, habilite:
   - **Message Content Intent**
   - **Server Members Intent**

## 3) Permissoes e intents necessarias

- Gateway intents usadas no codigo:
  - `guilds`
  - `voice_states`
  - `messages`
  - `message_content`
  - `members`
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
- `JULGAR_CHANNEL_ID`

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

## 5.2) Modulo Spotify do casal (opcional)

Painel do que cada um esta ouvindo, comparativo de escutas e resumo semanal, restrito a um
servidor, um canal e dois IDs de usuario.

| Comando | O que faz |
| --- | --- |
| `!conectar` | Envia no DM um link privado para autorizar a propria conta do Spotify |
| `!agora` | Reproducao atual das duas contas |
| `!top [@pessoa] [4-semanas\|6-meses\|1-ano]` | Top 10 do ranking do Spotify |
| `!comparar [semana\|passada]` | Reproducoes, top 5 de cada um e faixas em comum |
| `!minutos [hoje\|semana\|mes\|ano\|tudo]` | Tempo ouvido, combinando estimativa, medicao e historico real |
| `!importar` | Importa o Extended Streaming History do Spotify (anexe o zip) |
| `!desconectar` | Revoga a conexao e apaga os dados guardados |
| `!spotify` | Ajuda do modulo |

Automatico: painel fixado atualizado a cada 60s, coleta do historico recente a cada 2 minutos e
resumo semanal aos domingos as 20h de Brasilia.

O modulo so liga se `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `SPOTIFY_GUILD_ID`,
`SPOTIFY_CHANNEL_ID`, `SPOTIFY_USER_IDS` e `SPOTIFY_ENCRYPTION_KEY` estiverem definidos.
Sem isso o bot roda exatamente como antes.

> **Ranking do Spotify != escutas registradas pelo bot.** O `!top` vem pronto do Spotify;
> o `!comparar` e o resumo semanal contam so o que o bot registrou a partir do `!conectar`.

> **Minutos ouvidos nao existem em nenhum endpoint da API.** O `!minutos` combina tres
> fontes por precedencia: o historico real importado, a medicao do player enquanto o bot
> esta no ar, e a estimativa pela duracao das faixas. O embed sempre diz de onde veio cada
> parte do numero.

Passo a passo completo (cadastro do app, Premium exigido desde fev/2026, callback HTTPS,
permissoes, backup do SQLite e comportamento em falhas): **[docs/SPOTIFY.md](docs/SPOTIFY.md)**.

## 6) Observacoes sobre limitacoes da API (texto x voz)

- Presenca e estado de voz sao recebidos via **Gateway events**, nao via REST.
- Este projeto monitora eventos de **canal de voz** (`on_voice_state_update`) e comando de **texto** (`on_message`).
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
│   ├── julgar_listener.py
│   ├── calendar_auth.py
│   ├── calendar_client.py
│   ├── calendar_listener.py
│   ├── notion_client.py
│   ├── shift_manager.py
│   ├── shift_views.py
│   ├── task_views.py
│   ├── timer_manager.py
│   ├── spotify_auth.py       # OAuth por usuario + servidor de callback
│   ├── spotify_client.py     # chamadas a Spotify Web API
│   ├── spotify_format.py     # janelas de tempo (fuso BR) e embeds
│   ├── spotify_listener.py   # comandos, painel, coleta e resumo semanal
│   ├── spotify_store.py      # SQLite: tokens cifrados e escutas
│   ├── webhook.py
│   └── logger.py
├── docs/
│   └── SPOTIFY.md
├── tests/
├── .env.example
├── requirements.txt
├── requirements-dev.txt
├── docker-compose.yml
├── main.py
└── README.md
```

## Testes

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest
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

