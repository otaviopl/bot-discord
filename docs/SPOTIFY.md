# Spotify do casal

Módulo privado do bot: mostra o que cada um está ouvindo, compara rankings e publica um
resumo semanal. Restrito a um servidor, um canal e dois IDs de usuário.

---

## 1. O que o módulo faz

| Comando | O que responde |
| --- | --- |
| `!conectar` | Manda no seu DM um link privado para autorizar sua conta do Spotify |
| `!agora` | Reprodução atual das duas contas |
| `!top [@pessoa] [período]` | Top 10 músicas e artistas — **ranking do Spotify** |
| `!comparar [semana\|passada]` | Reproduções, top 5 de cada um e faixas em comum — **escutas registradas pelo bot** |
| `!minutos [hoje\|semana\|passada\|mes\|mes-passado\|ano\|tudo]` | Tempo ouvido (padrão: mês) |
| `!importar` | Importa o histórico real do Spotify (anexe o zip) |
| `!desconectar` | Revoga a conexão local e apaga os dados guardados daquela pessoa |
| `!spotify` | Ajuda do módulo |

Automático:

- **Sintonia** — quando as duas contas estão tocando a mesma faixa ao mesmo tempo, o bot
  avisa no canal. Faixas diferentes do mesmo artista geram um aviso mais fraco. Roda no
  tick do painel, sem requisição extra; pausado não conta. Uma mesma faixa não repete o
  aviso por 3 horas, e um mesmo artista por 8 — senão ouvir um álbum junto viraria um
  aviso por faixa. A trava fica no banco, então reiniciar o bot não repete nada.
- **Painel fixado** no canal. Só edita a mensagem quando o conteúdo muda.
- **Coleta** do histórico recente a cada 15 minutos.
- **Resumo semanal** aos domingos, 20h de Brasília, cobrindo os 7 dias anteriores.

Períodos aceitos em `!top`: `4-semanas` (padrão), `6-meses`, `1-ano`.
Períodos aceitos em `!comparar`: `semana` (atual, padrão) e `passada` (anterior).
A semana vai de segunda 00:00 a domingo 23:59:59, no fuso de Brasília.

### Duas contagens diferentes — não misture

| | Ranking do Spotify (`!top`) | Escutas registradas (`!comparar`, resumo semanal) |
| --- | --- | --- |
| Origem | Calculado pelo Spotify | Contado pelo bot, a partir do histórico recente |
| Cobertura | Todo o seu histórico na plataforma | **Só a partir da data em que você usou `!conectar`** |
| Precisão | O que o Spotify expõe em `short/medium/long_term` | Pode ter lacunas |

O endpoint de [histórico recente](https://developer.spotify.com/documentation/web-api/reference/get-recently-played)
devolve no máximo as últimas 50 faixas. Se vocês ouvirem mais de 50 músicas entre duas coletas
(intervalo de 15 minutos), as mais antigas se perdem — 50 faixas são ~2h30 de música,
então na prática isso não acontece. Mas é por isso
que o rodapé dos embeds avisa que pode haver lacunas.

O bot **não** tem acesso a minutos exatos de escuta, dados do Wrapped, nem ao histórico anterior
à conexão. Nada disso é exibido como se estivesse disponível.

---

## 1.1 Tempo ouvido: de onde vem o número

**Nenhum endpoint da Spotify Web API devolve minutos ouvidos.** Isso é dado de Wrapped,
que nunca esteve na API pública. O `!minutos` monta o número a partir de três fontes,
combinadas por precedência.

| # | Fonte | Precisão | Cobertura | Custo |
| --- | --- | --- | --- | --- |
| 1 | **Estimado** — duração da faixa, limitada ao intervalo até a escuta seguinte | Aproximada | Tudo desde o `!conectar` | Zero: usa a coleta que já existe |
| 2 | **Medido** — avanço real de `progress_ms` do player | Boa | Só enquanto o bot está no ar | Zero: usa o tick do painel |
| 3 | **Exato** — `ms_played` do Extended Streaming History | Real | Até a data do arquivo | Import manual |

### Como as três se combinam

Cada escuta vira um intervalo de tempo. As fontes são aplicadas da melhor para a pior, e
o período que uma fonte cobre é **subtraído** das seguintes. Assim um mesmo minuto nunca
é contado duas vezes, e sempre entra pela melhor fonte que existir para aquele momento:

```
tempo:     |------- manhã -------|---- tarde ----|--- noite ---|
importado: |#####################|               |
medido:    |     (descartado)    |###############|
estimado:  |     (descartado)    |  (descartado) |#############|
                   ↓                     ↓              ↓
resultado:      exato                 medido        estimado
```

O embed diz a composição — "1h do histórico do Spotify · 30min medidos · 15min estimados" —
e só omite o `≈` quando não sobra nenhuma parte estimada.

### Por que a camada 1 não soma a duração cheia

Somar `duration_ms` de cada faixa superestima quem pula música: uma faixa de 3 minutos
abandonada aos 40 segundos contaria 3 minutos. Por isso cada escuta é limitada ao intervalo
até a escuta seguinte — se a próxima faixa começou 40 segundos depois, contam 40 segundos.
A última escuta da janela, que não tem próxima, usa a duração cheia.

Onde essa camada ainda erra: pausar no meio de uma faixa e voltar horas depois. A camada 2
corrige exatamente esses casos enquanto o bot está no ar.

### Camada 2, em detalhe

A cada 60 segundos o painel já consulta o player. A mesma resposta traz `progress_ms`, então
a medição não custa nenhuma requisição a mais. Entre duas amostras da mesma faixa, o tempo
ouvido é o quanto o progresso avançou:

- **Pausado** — o progresso não anda, então nada é somado.
- **Faixa pulada** — só o que tocou de fato entra.
- **Arrastar a barra para frente** — o avanço é limitado ao tempo de relógio decorrido,
  então um pulo de 3 minutos não vira 3 minutos ouvidos.
- **Restart do bot** — a sessão é gravada a cada amostra, então o que já foi medido não se perde;
  só fica o buraco do tempo em que o bot esteve fora, que a camada 1 cobre.

### Camada 3: o import

O Spotify entrega, mediante pedido, o **Extended Streaming History**: um zip de JSONs com o
`ms_played` real de cada reprodução — o mesmo dado do Wrapped, cobrindo anos.

1. Spotify → Conta → **Privacidade**
2. Marque **Extended streaming history** (não o histórico curto de 1 ano)
3. Confirme pelo e-mail; o arquivo chega em até 30 dias
4. No Discord: `!importar` com o zip anexado (limite de 25 MB — se passar, mande os
   `.json` de dentro dele separados)

O parser aceita o formato atual (`Streaming_History_Audio_*.json`) e o antigo
(`StreamingHistory*.json`), ignora podcasts e descarta reproduções abaixo de 1 segundo.
Reimportar o mesmo arquivo não duplica nada.

> No arquivo, o campo `ts` marca quando a faixa **parou** de tocar — o início é calculado
> para trás a partir de `ms_played`. É por isso que o import consegue se encaixar
> corretamente na linha do tempo junto das outras fontes.

### O que o bot não faz

Não inventa histórico anterior à conexão sem o import, não mostra minutos como se fossem
exatos quando são estimados, e não busca dados de Wrapped — que não existem na API.

---

## 1.2 Cota do Spotify

Em Development Mode, além do rate limit de 30 segundos, existe desde julho de 2026 uma
**cota por conta de desenvolvedor** — somada entre **todos os apps** da mesma conta. O
Spotify não publica o número. Quando ela estoura, a resposta é `429` com
`reason: QUOTA_EXCEEDED` e um `Retry-After` de horas (em produção já vimos 33.715 s, ou
9h22). O bloqueio vale para o app inteiro, não para o comando nem para a pessoa.

O bot foi desenhado em torno disso:

| Mecanismo | O que evita |
| --- | --- |
| Bloqueio compartilhado | Depois de um 429, nenhuma chamada sai até o prazo acabar — nem de comando |
| Bloqueio persistido no banco | Um restart (todo deploy) esquecer o bloqueio e voltar a bater |
| Polling adaptativo do painel | Consultar a cada minuto quem nem está ouvindo |
| Coleta a cada 15 min | `recently-played` guarda 50 faixas (~2h30), então 15 min não perde nada |
| Pausa de 30 min após 403 | Insistir numa conta que o Spotify está recusando |
| Cache de 6h do `!top` | O Spotify recalcula os tops no máximo uma vez por dia |

Cadência do painel, por pessoa:

| Estado | Próxima consulta |
| --- | --- |
| Tocando | 60 s |
| Pausado | 2 min |
| Parado, anúncio ou podcast | 5 min |
| Spotify fora do ar | 2 min |
| Recusado (403) | 30 min |

Consumo estimado, para duas pessoas que ouvem ~4 h por dia: **~1.150 chamadas/dia**,
contra ~4.400 do desenho anterior.

Durante um bloqueio, o painel ganha um aviso no rodapé com o horário de volta, e os
comandos respondem dizendo quando o Spotify libera em vez de mostrar segundos crus.
`!minutos`, `!comparar` e o resumo semanal continuam funcionando, porque só usam o banco.
O `!top` serve o último ranking guardado, se houver.

> Se você tiver outros apps na mesma conta de desenvolvedor do Spotify, eles gastam da
> mesma cota.

---

## 2. Cadastrar o app no Spotify

1. Acesse o [Dashboard do Spotify for Developers](https://developer.spotify.com/dashboard) e crie um app.
2. O app fica em **Development Mode**. Desde a
   [migração de fevereiro de 2026](https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide),
   isso implica:
   - **O dono do app precisa manter Spotify Premium ativo.** Se o Premium cair, o app para de funcionar.
   - Limite de **5 usuários** por app — suficiente para duas contas.
3. Em **Settings → User Management**, cadastre **nome e e-mail** das duas contas do Spotify
   que vão usar o bot. Esse passo é obrigatório e silencioso: uma conta que não está ali
   **consegue autorizar normalmente** — a tela do Spotify aparece, o token é emitido, o bot
   confirma "Spotify conectado" — e só depois é barrada, com `403 User not registered in the
   Developer Dashboard` em toda chamada de API.

   Se alguém autorizou e mesmo assim o bot diz que a conta não está liberada, é esse cadastro
   que falta. Reconectar não adianta; cadastre a conta e rode `!conectar` uma vez depois.
4. Em **Settings → Redirect URIs**, registre **exatamente** o valor de `SPOTIFY_REDIRECT_URI`
   (byte a byte, incluindo `https://` e o path).
5. Copie **Client ID** e **Client Secret** para o `.env`.

Escopos solicitados (só o necessário):

- `user-read-currently-playing` — reprodução atual
- `user-read-recently-played` — histórico recente
- `user-top-read` — rankings

O bot **não** pede permissão para controlar a reprodução nem para ler ou alterar playlists.

---

## 3. Callback HTTPS

O bot sobe um servidor HTTP próprio dentro do container, em `SPOTIFY_OAUTH_PORT` (padrão `8888`),
que atende duas rotas:

- `GET /spotify/callback` — recebe o retorno do Spotify
- `GET /health` — responde `ok`, útil para healthcheck do proxy

O Spotify exige HTTPS no redirect URI (exceto em loopback `127.0.0.1`), então o proxy do seu
servidor precisa encaminhar o domínio público para essa porta.

### Este bot, no Dokploy da Teitas

O bot roda como **Compose** no Dokploy (`76.13.227.197`):

| | |
| --- | --- |
| Projeto | `bot-discord` |
| Serviço | `Bot` — appName `botdiscord-bot-dhoste` |
| Origem | `otaviopl/bot-discord`, branch `main`, **auto-deploy no push** |
| Serviço no compose | `discord-voice-watcher-bot` |
| Domínio | **precisa ser criado** — não havia nenhum |

Domínio sugerido: `spotify.teitas.com.br` (o `bot.teitas.com.br` já é do TeitasBot).
Não existe wildcard em `teitas.com.br`, então é preciso criar o registro A:

```
Tipo: A   Nome: spotify   Valor: 76.13.227.197   TTL: 300
```

E no Dokploy, em **Bot → Domains → Add Domain**:

```
Host:         spotify.teitas.com.br
Service Name: discord-voice-watcher-bot
Path:         /
Port:         8888
HTTPS:        on
Certificate:  Let's Encrypt
```

Com isso, o valor a registrar no Spotify e no `.env`:

```
SPOTIFY_REDIRECT_URI=https://spotify.teitas.com.br/spotify/callback
```

> O `docker-compose.yml` declara a rede externa `dokploy-network` — sem ela o Traefik não
> enxerga o container e o domínio devolve 404. Para rodar o compose fora do Dokploy,
> crie a rede antes: `docker network create dokploy-network`.

### Dokploy / Traefik (caso geral)

Aponte um domínio (ou um path) para o container na porta `8888`. Com o domínio
`spotify.seudominio.com`, o `.env` fica:

```
SPOTIFY_REDIRECT_URI=https://spotify.seudominio.com/spotify/callback
SPOTIFY_OAUTH_PORT=8888
```

E o mesmo valor vai em **Redirect URIs** no dashboard do Spotify.

### Nginx

```nginx
location /spotify/ {
    proxy_pass http://127.0.0.1:8888;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

### Testar sem domínio

Para experimentar localmente, o Spotify aceita loopback:

```
SPOTIFY_REDIRECT_URI=http://127.0.0.1:8888/spotify/callback
```

Registre esse mesmo valor no dashboard. Funciona só na máquina onde o bot roda.

---

## 4. Configurar o `.env`

Gere a chave que cifra os tokens no banco — **uma única vez**:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Trocar essa chave depois invalida todos os tokens já salvos e obriga as duas pessoas a rodar
`!conectar` de novo (o bot detecta isso e segue funcionando, sem quebrar).

Preencha no `.env` (veja `.env.example` para o arquivo completo):

```
SPOTIFY_CLIENT_ID=...
SPOTIFY_CLIENT_SECRET=...
SPOTIFY_REDIRECT_URI=https://spotify.seudominio.com/spotify/callback
SPOTIFY_OAUTH_HOST=0.0.0.0
SPOTIFY_OAUTH_PORT=8888
SPOTIFY_GUILD_ID=...        # ID do servidor
SPOTIFY_CHANNEL_ID=...      # ID do canal privado do casal
SPOTIFY_USER_IDS=...,...    # os dois IDs de usuário, separados por vírgula
SPOTIFY_DB_PATH=/data/spotify.db
SPOTIFY_ENCRYPTION_KEY=...
```

Para descobrir os IDs: ative o **Modo desenvolvedor** no Discord
(Configurações → Avançado) e use "Copiar ID" no servidor, no canal e em cada pessoa.
O comando `!servers`, que o bot já tinha, também lista os IDs dos canais.

Se qualquer uma dessas variáveis estiver faltando, o módulo do Spotify fica desligado e o resto
do bot (Notion, Calendar, turnos, timers) continua funcionando normalmente.

---

## 5. Permissões no Discord

O bot precisa, **no canal do casal**:

- Ver Canal
- Enviar Mensagens
- Inserir Links (embeds)
- Gerenciar Mensagens (para fixar o painel)
- Ver Histórico de Mensagens (para recuperar o painel depois de um restart)

As intents já habilitadas no bot (`message_content`, `members`) são suficientes — não é preciso
mudar nada no Developer Portal do Discord.

---

## 6. Subir

```bash
docker compose --env-file .env up -d --build
```

Acompanhe:

```bash
docker compose logs -f
```

No boot, procure a linha `Spotify integration enabled` no log. Se aparecer
`Spotify integration disabled`, alguma variável obrigatória está faltando.

Depois de subir, cada pessoa manda `!conectar` no canal (ou no DM do bot) e abre o link que
chega no DM. A contagem de escutas começa nesse momento.

---

## 7. Backup

Tudo do módulo mora em um único arquivo SQLite dentro do volume `bot-data`:
tokens cifrados, escutas registradas, ID do painel e marcas dos resumos já publicados.

Backup a quente (seguro com o bot rodando, graças ao WAL):

```bash
docker compose exec discord-voice-watcher-bot \
  python -c "import sqlite3; sqlite3.connect('/data/spotify.db').backup(sqlite3.connect('/data/backup.db'))"
docker compose cp discord-voice-watcher-bot:/data/backup.db ./spotify-$(date +%F).db
```

Restaurar:

```bash
docker compose stop
docker compose cp ./spotify-2026-09-09.db discord-voice-watcher-bot:/data/spotify.db
docker compose start
```

> O backup contém os tokens cifrados, mas **não** a chave. Guarde `SPOTIFY_ENCRYPTION_KEY`
> separado do backup — sem ela o arquivo é inútil, e com as duas juntas alguém teria acesso
> às contas. Não versione nenhum dos dois.

---

## 8. Comportamento em falhas

| Situação | O que acontece |
| --- | --- |
| Nada tocando | Painel mostra "Sem reprodução ativa" |
| Música pausada | Painel mostra "Pausado" com a faixa |
| Anúncio ou podcast | Painel identifica e não registra como música |
| Spotify fora do ar | Mantém o último dado conhecido com o horário: "Dados indisponíveis · último às 14:32" |
| Rate limit ou cota (429) | Bloqueia todas as chamadas até o prazo, persiste o bloqueio no banco e avisa no painel quando volta |
| Autorização revogada (401) | Marca a conta, mostra "rode `!conectar`" e para de consultar aquela pessoa |
| Conta não cadastrada no app (403) | Explica que falta o cadastro em User Management; **não** pede reconexão, porque reconectar não resolve |
| Painel apagado | Recria e fixa na próxima atualização |
| Restart do serviço | Histórico preservado, painel recuperado pelo ID salvo, resumo da semana não republica |
| Terceiro usando os comandos | Recusa com "Bot privado" e registra no log |

---

## 9. Rodar os testes

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest
```

A suíte cobre dedupe de escutas, cifragem dos tokens, renovação e revogação de autorização,
os estados do player, virada de semana no fuso de Brasília, sobrevivência a restart e o
fluxo completo de OAuth com servidor HTTP real.

Para o tempo ouvido: precedência entre as três fontes sem contagem dupla, faixa pulada,
pausa, scrub, os dois formatos de arquivo do Spotify, proteção contra zip bomb e a migração
de bancos criados antes da coluna de duração existir.
