# Instalador com reversão, candidato em revisão

Risco VERMELHO. Não executado na instalação real. A revisão cruzada e o smoke do
painel são obrigatórios antes de usar `--apply`. Não usa o `install.sh` antigo.

```sh
python3 -B -m castanha.deployment --candidate /caminho/da/release --sha SHA_COMPLETO
# Após revisão e provas do mesmo SHA:
python3 -B -m castanha.deployment --candidate /caminho/da/release --sha SHA_COMPLETO --apply
# Recuperação pelo journal, inclusive após interrupção do instalador:
python3 -B -m castanha.deployment --rollback
```

O preflight é somente leitura. Exige links CLI/plugin coerentes, checkouts limpos,
SHA do candidato, manifest válido, captura ociosa sem processo de captura vivo, e
daemon identificado por usuário,
executável, cwd, boot e instante de criação. Não usa PID como identidade suficiente.
O sinal de encerramento usa pidfd, que prende a identidade no kernel. Não usa SIGKILL.

Antes da mutação, grava `deployment.json` no estado Castanha. Cada link troca por
rename atômico; os dois caminhos não são uma operação atômica de filesystem. O
journal registra o conjunto e permite compensar uma troca parcial. Falha de smoke
restaura os dois links e o daemon anterior. Erro na reversão fica explícito, não vira
sucesso. Configuração, originais, checkpoints e worktrees não são apagados.

Provas: 25 testes, incluindo captura ativa, estado corrompido, identidade
divergente, troca parcial, falha de saúde/start/rollback, morte antes do segundo
link e recuperação. Um teste usa processo Python isolado real e pidfd: identidade
divergente não recebe sinal, identidade correta encerra somente a fixture. Outro
recusa PID/boot/cwd/exe/uid divergentes e um pidfile vivo apontando para outro
checkout. A identidade do daemon instalado no XPS foi conferida somente em leitura.

## A corrida com o start da captura

`castanha.engine.start_recording` lê o estado, gasta tempo fora de qualquer trava
(no produto é o `pactl` de `is_default_source_muted`), sobe o ffmpeg e só então
publica o PID em `state.json`. Ou seja, `assert_idle` sozinho pode ler "ocioso"
enquanto já existe gravação nascendo. `assert_quiescent` acrescenta duas provas e
é repetido imediatamente antes de cada mutação:

1. varredura de `/proc` por processo vivo do mesmo usuário com o arquivo de saída
   da captura no argv (`castanha_rec_*.ogg`), que existe desde o Popen, antes da
   publicação do estado;
2. impressão digital do `state.json` (dev, inode, mtime, tamanho, sha256) tirada
   logo depois do stop do daemon e reconferida antes de cada troca de link. Nessa
   janela não sobra escritor legítimo, então qualquer write é captura nascendo.
   Depois que o daemon novo sobe, a digital deixa de valer, porque ele publica
   agenda e o retry automático pode normalizar um finalizador morto, e só ficam de
   pé o ocioso e a varredura de processo. Nenhum desses caminhos declara gravação,
   então a checagem pós-smoke não reverte instalação sã.

**Fechado:** captura declarada em qualquer ponto entre o preflight e o fim da
instalação, e captura visível como processo em qualquer um dos pontos de checagem.
Nos dois casos a troca é recusada ou revertida, com os dois links de volta ao
checkout anterior e o daemon anterior reiniciado. A reversão deixou de exigir
ocioso: com gravação viva, devolver os links é obrigatório, porque o par meio
trocado é o pior estado possível para quem gravou no meio da interrupção.

**Residual, e só fecha fora deste ownership:** a janela entre a decisão da captura
(leitura do estado devolvendo ocioso) e o Popen do ffmpeg não tem artefato
observável, nem processo nem write. Se a instalação inteira couber nessa janela, a
gravação nasce sobre a instalação nova e o instalador não teve como ver. O teste
`test_capture_decided_before_the_install_is_the_residual_hole` reproduz a
interleaving exata com processo e barreiras de verdade e é contraprova durável, não
teste de fumaça. Existe um segundo ponto de nascimento pelo mesmo motivo: a thread
`_trigger_meeting_alert` do daemon chama `start_recording` ao clique da notificação,
inclusive depois de o SIGTERM do instalador ter chegado ao laço principal.

**Mudança mínima que fecha, um arquivo só, `castanha/engine.py`:** `start_recording`
toma `get_state_dir()/".deployment.lock"` com `LOCK_EX | LOCK_NB` do começo da
leitura do estado até depois de publicar o PID, e recusa a gravação com mensagem
clara quando a trava está tomada. É a mesma trava que o instalador já toma durante
toda a mutação. Os dois lados estão provados aqui nas duas ordens:
`test_capture_holding_the_shared_lock_makes_the_install_refuse_honestly` (captura
primeiro, instalação recusa honesto, sem traceback) e
`test_install_holding_the_shared_lock_would_refuse_a_lock_aware_capture` (instalador
primeiro, captura ciente da trava não nasce). Falta apenas o lado do `engine.py`.

Pendências antes de ativar:

- Revisão adversarial do instalador e do código de release correspondente.
- A trava do lado da captura, acima. Sem ela, não declarar F5P pronto nem fazer
  instalação automática.
- O instalador precisa rodar sob o mesmo interpretador do daemon, porque
  `LocalPlatform.daemon` compara `exe` com `sys.executable` resolvido. No XPS o
  baseline é `/usr/bin/python3.14`.
- O smoke de processo/CLI e rescan não prova renderização do painel. Repetir Qt,
  PipeWire sintético e verificação visual na release real.
- Testar inicialização/falha do daemon completo em ambiente isolado, sem agenda,
  credenciais ou áudio pessoal. Os testes da transação usam plataforma controlada.
