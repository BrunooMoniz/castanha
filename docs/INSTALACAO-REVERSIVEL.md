# Atualização do checkout único

No XPS consolidado, CLI e plugin apontam para `~/Projects/castanha` e o
daemon roda em `castanha.service` do systemd do usuário. Após testes e revisão,
aplique o commit local exato a partir da worktree de desenvolvimento:

```sh
python3 scripts/deploy-local.py ~/Projects/castanha SHA_COMPLETO
```

O script exige árvore limpa e captura ociosa, mantém os links existentes,
atualiza por fast-forward e confere serviço, PID, checkout e resposta do CLI.
Se a saúde falhar, restaura o commit anterior e reinicia o serviço.
O SHA anterior fica em `~/.local/state/castanha/local-release.json` antes da
troca. Gravações e configuração não são alteradas pelo instalador.
O código privado de integração permanece local, com backup por bundle privado.

## Histórico: instalador de transição entre worktrees

A recuperação registra `restored_daemon` e `rollback_checking` antes de testar
a saúde do daemon antigo reiniciado. Uma interrupção nesse teste pode ser
retomada sem iniciar outra instância. A identidade completa continua obrigatória.
Na janela residual entre criar o processo e persistir sua identidade, uma queda
continua recusando a recuperação automática se houver PID vivo não registrado;
não se adota um processo apenas por seu diretório. Essa recusa exige diagnóstico
do processo, não limpeza do pidfile nem nova instalação por cima do journal.

Risco **VERMELHO na instalação**. F5PT foi desenvolvido sobre `2508867` e
F5PT2 continua sobre `b774ad6`, na mesma worktree Orca isolada. Não foi instalado, não recebeu sinal nenhum processo
do usuário, e o XPS não foi alterado. A revisão Codex fresca sob waiver do coordenador e o smoke da
release continuam pendentes antes de instalação real. Não usa `install.sh`.

```sh
# Sempre rodar a partir do checkout CANDIDATO, com o interpretador do daemon.
# Preflight somente leitura da primeira transição:
python3 -B -m castanha.deployment --candidate /release --sha SHA_COMPLETO --initial-transition
# Aplicação depois das provas e revisão da mesma release:
python3 -B -m castanha.deployment --candidate /release --sha SHA_COMPLETO --initial-transition --apply
# Reversão, inclusive após interrupção abrupta:
python3 -B -m castanha.deployment --rollback
```

`--initial-transition` aceita exclusivamente
`b97131edbb18cbca0e11b959dc8f92a4b52fa171`. Sem a opção, permanece a recusa
`LegacyTransitionBlocked` para runtime sem trava. Releases posteriores continuam
exigindo a prova comportamental de cooperação com `.deployment.lock`.

## Cerca inicial e fronteira administrada

O b97131e ignora flock. Trocar apenas o link CLI não fecha chamadas pelo checkout.
F5PT troca **o diretório `checkout/castanha` inteiro**, por uma única syscall Linux
`renameat2(RENAME_EXCHANGE)`, com um pacote bloqueador previamente sincronizado.
A troca é atômica, inclusive para fontes e `__pycache__`. Não existe fallback com
duas renomeações. Kernel, libc ou filesystem sem suporte recusam a transição.

Os entrypoints administrados convergem nesse pacote:

- CLI instalado, inclusive painel, atalho e chamada pelo PATH;
- `checkout/bin/castanha` e `python bin/castanha`, com caminhos absolutos ou relativos;
- `plugin/bin/castanha`, pelo symlink instalado do plugin;
- `python -m castanha.*` selecionando esse checkout e execução direta dos módulos
  Python do pacote, que dependem dos imports dele.

Todos os comandos desses entrypoints ficam indisponíveis durante a primeira
transição, inclusive leituras de status. O pacote bloqueador recusa com saída não
zero antes de carregar engine/configuração; módulos diretos ausentes também
falham. Essa primeira cerca não promete código 75, reservado ao bloqueador CLI
das transições cooperativas.

Links CLI/plugin precisam apontar diretamente para o checkout conhecido, sem
cadeias de aliases; links relativos diretos são preservados literalmente. O pacote
e o CLI devem ter os bytes reais da revisão permitida. Symlinks internos, hardlinks,
arquivos extras, donos e permissões inesperados recusam a operação. Caches só são
aceitos se forem do interpretador em uso e seu código compilado equivaler à fonte
verificada, incluindo o nível de otimização; cache divergente não é apagado para
fazer o teste passar. A mesma validação aceita caches produzidos pelo bloqueador.
Ancestrais precisam ser diretórios reais sem escrita por terceiros; `/tmp` de root
com sticky é permitido para fixtures. Montagens incompatíveis falham na syscall.

**Fora da fronteira:** cópia arbitrária de código, virtualenv com outra cópia,
importador próprio que conserve código em memória e disfarce sua origem,
`PYTHONPATH`/hooks que substituam a resolução normal, exec via descritor arbitrário,
execução pelo backup privado, root malicioso ou adulteração concorrente pelo mesmo
usuário. A cerca não é um sandbox de segurança contra o dono dos arquivos.
Alterações observadas em caminhos, bytes, inodes, modos ou links recusam; não se
alega exclusão de um adversário com autoridade para trocar esses objetos depois
da verificação. O backup não é um entrypoint e não deve ser usado para executar.

## Quem abriu o legado antes da cerca

O exchange não invalida descritores abertos nem código já carregado. Por isso a
ordem obrigatória é: cerca durável, parada identificada do daemon, inventário de
leitores, preflight final de captura, e somente então swap da release. O inventário
usado para reconhecer caminhos dos módulos vem do recibo anterior ao exchange: o
`daemon.py` original já não existe no caminho atual. Isso cobre
`python /checkout/castanha/daemon.py`, os equivalentes pelo plugin e caminhos
relativos, inclusive quando o cwd é externo e os FDs do código já foram fechados.

O inventário é **uma recusa imediata**, sem contagem de scans limpos e sem polling
como garantia. Reconhece argv absoluto/relativo, módulos `-m castanha.*` mesmo de
outro cwd, processos dentro do checkout/backup e descritores abertos nessas
árvores. Um shell ainda dentro do checkout também recusa conservadoramente.
Nenhum CLI em voo ou captura é sinalizado para forçar quiescência.

A inspeção de cwd/descritores abrange intérpretes Python/PyPy e shells, além
dos entrypoints explícitos reconhecidos pelo argv. Serviços nativos do desktop,
como systemd --user, não exigem acesso ptrace: filhos que executem o Castanha
passam por exec e pela cerca. Importadores próprios, Python embarcado em outro
executável e argv deliberadamente disfarçado continuam fora da fronteira,
como as cópias arbitrárias de código. Um intérprete relevante ilegível continua
recusando a instalação; não se altera a segurança de /proc para instalar.

A justificativa depende dos bytes auditados de b97131e: eles não fazem fork de
runtime Python carregado. Filhos do CLI/daemon passam por exec e reimportam a
cerca, ou são processos externos. No nascimento da captura, `Popen` só retorna
ao pai depois do exec do ffmpeg. Portanto um start anterior ainda em voo aparece
como leitor; se o pai já saiu, a captura persistente aparece na varredura seguinte
ou no estado. A lista de `/proc` para captura é obtida **depois** de concluir o
inventário de leitores. Novos starts normais posteriores ao exchange não podem
carregar o pacote antigo. Essa barreira, junto à propriedade do legado auditado,
fecha a janela; repetir observações por alguns segundos sozinho não fecharia.

Enumerar `/proc`, ler cwd/argv/fds ou confirmar desaparecimento com erro não prova
ausência. A operação recusa. Só desaparecimento confirmado do PID é ignorado;
cwd apagado com PID ainda presente é erro. Um FD que fechou entre enumeração e
`readlink` pode desaparecer normalmente. A varredura continua restrita ao mesmo
usuário, identidade exigida também para os arquivos e daemon administrados.

O preflight final exige estado ocioso, nenhum processo de captura vivo e uma
impressão digital de `state.json` (dev/inode/mtime/tamanho/hash). A digital é
reconferida ao trocar o plugin. Depois do start do daemon novo, escritas legítimas
de agenda tornam a digital inadequada; continuam estado ocioso e ausência de
captura. A integridade da cerca é reconferida antes do swap e antes de concluir.

## Plano de reversão e durabilidade, antes do ato

Antes de cercar, o instalador grava `deployment.json` e o recibo separado
`transition-fence.json`, modo 0600, no diretório de estado. O recibo registra
caminhos, alvos literais dos links, revisão, inodes/dev, hashes, donos e modos.
Os arquivos originais e diretórios são sincronizados; bloqueador, recibos e
renomeações têm fsync de arquivo/diretório conforme a operação.

A aplicação modifica **deliberadamente** o pacote `checkout/castanha` do checkout
legado administrado. Esse checkout fica modificado no git enquanto a cerca vigora;
seu pacote original passa a um backup irmão no mesmo filesystem. Essa mutação
pertence exclusivamente à aplicação explícita da primeira transição. As provas
F5PT2 executaram essa troca somente em checkouts descartáveis.

O pacote original não é reescrito. O exchange o preserva inteiro em
`.castanha-fence-<uuid>`, irmão do checkout, com seus inodes, bytes, modos e caches.
`bin/castanha` permanece intacto. Em sucesso, o checkout antigo **continua
cercado**: restaurá-lo nesse momento reabriria starts não cooperativos durante a
release nova. CLI e plugin passam a apontar ao candidato, que coopera com a trava.

Em erro ou `--rollback`: parar somente o daemon candidato identificado, devolver
ambos os links aos alvos anteriores enquanto o pacote antigo continua cercado,
inverter o exchange, conferir o checkout restaurado e só então reativar o daemon
anterior quando houver parada confirmada. Os links restaurados preservam o texto
do alvo e seu modo; seus inodes podem mudar com o rename. Uma parada incerta não
autoriza iniciar outro daemon. Erro de reversão fica explícito no journal.

A recuperação decide pela identidade e conteúdo em disco, e não apenas pela fase:
um crash entre exchange e atualização do recibo pode deixar qualquer orientação
dos dois diretórios. Repetir a restauração é idempotente. Recibo, stub ou backup
alterados recusam a recuperação, sem sobrescrever o objeto inesperado. O recibo
fica fora do checkout. Diretórios auxiliares de tentativas interrompidas são
preservados, sem limpeza destrutiva automática.

F5PT2 distingue pidfile obsoleto de identidade viva ilegível durante a recuperação.
Somente `ESRCH` de `pidfd_open` ou prontidão do pidfd preso ao processo provam
encerramento, inclusive de um zombie. Assim, o pidfile de um processo morto permite
devolver pacote e links mesmo quando as consultas normais do candidato e do
legado recusam a identidade. Não se remove o pidfile para fabricar ausência.
`PermissionError`, filho ilegível/ausente de `/proc` com processo vivo e erro no
pidfd continuam recusando. Um PID vivo precisa coincidir integralmente com a
identidade registrada no journal para aquele checkout; PID reutilizado ou daemon
sem identidade registrada não recebem sinal nem liberam a cerca. A leitura do
pidfile também distingue ausência de arquivo, permissão negada e link pendente.

A trava compartilhada abre com `O_NOFOLLOW`, valida arquivo regular, dono,
permissões e link único. Não segue symlink nem aceita hardlink para um inode que
outro caminho poderia usar como trava diferente.

## Evidência e pendências

`tests/test_transition_fence.py` extrai **b97131e real** em checkout temporário.
Executa CLI/start reais com HOME/XDG isolados e áudio/notificação substituídos,
sem serviço, áudio, dados ou sinais do usuário. A prova inclui entrypoints diretos,
CLI aberto antes do exchange, start já carregado parado antes da captura, leitor
reconhecido só pelo FD, caches legítimos/adulterados, erro de `/proc`, permissões,
links, fsync, falha de saúde, sucesso mantendo a cerca e rollback completo.

Crashes de processo com `os._exit` exercitam dez fases: `preparing`, `slot_created`,
`stub_written`, `prepared`, `exchanging`, `exchanged`, `fenced`, `restoring`,
`restore_exchanged` e `restored`. Cada fase exige recibo durável e duas restaurações
com os mesmos bytes, modos e inodes do pacote. Outra matriz mata o instalador
integrado em nove pontos, incluindo cada swap de link, e exige recuperação dupla
pelo journal com ambos os links anteriores. Isso simula morte do instalador,
não corte físico de energia; fsync e exchange fornecem o contrato de filesystem.

Baseline em `2508867`: 237 testes verdes. F5PT passou em 59 testes focados e
255 completos. F5PT2 acrescenta três testes com múltiplos cenários: daemon real
b97131e com cwd externo e sem FDs do checkout por cinco caminhos, crash recovery
com PID recolhido/zombie e recusa de PID vivo ilegível/reutilizado/não registrado.
Os dois defeitos foram reproduzidos contra `b774ad6` em checkout temporário antes
de validar a correção. Gates de entrega:

```sh
python3 -B -m unittest tests.test_transition_fence tests.test_deployment -q
python3 -B -m unittest discover -s tests -q
git diff --check
```

Antes de instalar: revisão Codex fresca sob waiver do SHA entregue, smoke do
daemon completo em ambiente isolado, Qt/painel e PipeWire sintético na release.
O instalador precisa usar o interpretador do daemon (baseline XPS:
`/usr/bin/python3.14`). Os testes da transação usam plataforma controlada; não são
prova de instalação ou renderização no XPS. AGY/OpenCode não foram usados.
Entrega por commit local e bundle privado 0600, nunca push ao origin público.
