# Biblioteca local de reuniões

`LibraryWindow { id: library }`

A janela começa invisível. `library.showMeeting(slug)` abre o detalhe; `library.showMeeting("")` abre o histórico. Fechar interrompe somente a reprodução da biblioteca. Cores, fontes, arredondamento e bordas seguem os tokens dinâmicos do Omarchy. `background`, `accent` e `readingFontFamily` são opcionais. `cliCommand` e `clipboardCommand` permitem fixtures isoladas nos testes.

`castanha library --json` retorna todas as reuniões locais, com título, data, duração, disponibilidade e estado. `castanha library SLUG --json` acrescenta notas, decisões e próximos passos do Gold, texto integral, segmentos existentes e áudios originais. Falhas retornam JSON de erro e código diferente de zero. A consulta não usa rede, não executa LLM e não altera os arquivos das reuniões.

O resumo apresenta cabeçalhos com hierarquia e omite somente o primeiro H1 idêntico ao título da reunião. Itens extraídos do Gold só deixam de se repetir quando correspondem a uma linha inteira da seção apropriada das notas; conteúdo adicional permanece em Decisões extraídas/Ações extraídas. A cópia conserva o conteúdo completo.

A transcrição permite alternar entre segmentos com tempos e texto integral. Tempos só iniciam reprodução quando job, referência temporal e SHA-256 correspondem a um único áudio original e o hash real foi conferido. Canal local/remoto não é identidade de participante. Textos são PlainText, sem links automáticos; copiar envia o texto por stdin ao wl-copy.

Leitura limitada a 8 MiB por documento, com erro explícito; áudios não são carregados no inventário. Identificadores e componentes internos rejeitam travessia e symlinks. Os diretórios raiz configurados são confiáveis. Arquivos removidos ou trocados depois da leitura podem tornar a reprodução indisponível, com erro visível.

O estado utiliza os metadados locais. Sem confirmação conhecida de entrega, a reunião fica em Pendentes com indicação explícita, mesmo havendo notas. Fatos aguardando proveniência também mantêm pendência. Recibos antigos externos aos metadados não são reinterpretados pela biblioteca.

## Verificação

- `python3 -m unittest tests.test_library -q`: fixtures com HOME/XDG isolados, CLI real, bloqueio de caminhos externos, proveniência, estado de entrega e janela Quickshell real. Exige Wayland, Quickshell e QtMultimedia instalados, sem rede.
- `QT_QPA_PLATFORM=offscreen /usr/lib/qt6/bin/qmltestrunner -input tests/qml/tst_library.qml -o -,txt`: filtros, tempos, URLs locais e texto literal.

Risco AMARELO: nova interface e nova consulta local. Reversão: restaurar a revisão anterior pelo deployment do projeto; nenhum dado de reunião é migrado.

## Excluir gravações e reprocessar

Na aba Áudio, cada gravação tem Ouvir e Excluir. A exclusão exige confirmação, interrompe a reprodução e envia o identificador exato do arquivo e a revisão observada. Uma cópia recuperável fica na quarentena da reunião. A transcrição, o resumo e os insights anteriores deixam de ser apresentados como válidos. Minhas notas e a reunião são preservadas.

Reprocessar reunião usa somente os áudios restantes. Sem áudio válido, o botão fica desabilitado e a reunião não dispara transcrição ou síntese. Pendências de processamento e de retirada do conteúdo no Zinom continuam visíveis. Desfazer exclusão só aparece quando o backend confirma que a restauração ainda é permitida; o início de uma retirada remota torna essa restauração indisponível.

Risco VERMELHO para exclusão: testes de retomada, isolamento e preservação dos originais, mais revisão independente antes da entrega. A quarentena preserva o áudio e os snapshots locais. Tombstones remotos não são desfeitos pelo rollback de código.

## Mover gravação e vincular evento da agenda

Na aba Áudio, cada gravação tem **Mover…**: o destino é outra reunião (as do mesmo dia primeiro, depois as mais recentes) ou uma reunião nova com título. A gravação sai da origem pela mesma exclusão com quarentena (`recordings exclude`), e entra no destino como um job novo com a transcrição preservada, marcado `transcribed`: a retomada automática (`sync.auto_retry_enabled`) refaz a transcrição consolidada, o resumo e a entrega ao Zinom das duas reuniões sem novo ASR; com o retry desligado, `castanha sync --all` ou Reprocessar em cada reunião conclui, e a mensagem do movimento diz qual é o caso. Transcrição preservada de um áudio já apagado (`delete-recording`) conta como conteúdo restante da origem. A origem deixa de oferecer "Desfazer exclusão" (a cópia da quarentena fica), e o conteúdo que ela já tinha enviado ao Zinom é retirado pelo fluxo durável da exclusão. A síntese antiga do destino não é retirada, como já acontece quando uma segunda gravação é anexada a uma reunião. Gravação ainda sem transcrição, reunião em captura, congelada, com resumo em regeneração ou com alteração de áudios por concluir recusam o movimento sem alterar nada. O movimento corre sob a trava global de mutação (a mesma da captura e da exclusão) e, dentro dela, exclusão da origem, journal e anexo acontecem sob a trava da origem, sem soltá-la. As travas de reunião seguem uma ordem global (nome menor primeiro) e o destino é validado sob a própria trava. Um movimento interrompido depois do journal bloqueia captura nova na origem e é concluído pela retomada automática (`resume_move`) antes de a exclusão da origem seguir; ao vincular, a lista de convidados entra no corpo da nota para que o Zinom receba a mudança mesmo com o título igual.

No cabeçalho, **Vincular a evento…** lista as reuniões com hora do dia da gravação (`castanha agenda --dia AAAA-MM-DD --json`, todas as agendas selecionadas, escondidas inclusive) e aplica `castanha link-event`: a reunião passa a ter o título, os convidados e o link do evento no metadata, no frontmatter do Silver e no Gold. O resumo já escrito é mantido ("Reprocessar reunião" o refaz com os convidados); `recording_revision` não muda, porque as gravações não mudaram; o Zinom recebe a nova versão pela fila, porque o título altera a revisão do envelope.

CLI: `castanha recordings move SLUG ARQUIVO (--to=DESTINO | --new-title=TÍTULO [--event UID] [--date AAAA-MM-DD]) [--expected-revision N] --json`; `castanha link-event SLUG UID [--date AAAA-MM-DD] --json`.

Verificação: `python3 -m unittest tests.test_relocation tests.test_recording_actions_ui -q` e `QT_QPA_PLATFORM=offscreen /usr/lib/qt6/bin/qmltestrunner -input tests/qml/tst_library.qml`. Risco VERMELHO para mover (dado do usuário e retirada remota), AMARELO para vincular. Reversão do código: revisão anterior pelo mecanismo do projeto e reinício do daemon; um movimento concluído não tem desfazer, e a quarentena da origem preserva o áudio.
