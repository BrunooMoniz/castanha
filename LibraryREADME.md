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
