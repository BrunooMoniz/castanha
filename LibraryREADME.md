# Biblioteca local de reuniões

`LibraryWindow { id: library; foreground: root.foreground; fontFamily: root.fontFamily }`

A janela começa invisível. `library.showMeeting(slug)` abre o detalhe; `library.showMeeting("")` abre o histórico. Fechar interrompe somente a reprodução da biblioteca. `background`, `accent` e `readingFontFamily` são opcionais; o texto de leitura usa Sans. `cliCommand` e `clipboardCommand` permitem fixtures isoladas nos testes.

`castanha library --json` retorna todas as reuniões locais, com título, data, duração, disponibilidade e estado. `castanha library SLUG --json` acrescenta notas, decisões e próximos passos do Gold, texto integral, segmentos existentes e áudios originais. Falhas retornam JSON de erro e código diferente de zero. A consulta não usa rede, não executa LLM e não altera os arquivos das reuniões.

A transcrição permite alternar entre segmentos com tempos e texto integral. Tempos só iniciam reprodução quando job, referência temporal e SHA-256 correspondem a um único áudio original e o hash real foi conferido. Canal local/remoto não é identidade de participante. Textos são PlainText, sem links automáticos; copiar envia o texto por stdin ao wl-copy.

Leitura limitada a 8 MiB por documento, com erro explícito; áudios não são carregados no inventário. Identificadores e componentes internos rejeitam travessia e symlinks. Os diretórios raiz configurados são confiáveis. Arquivos removidos ou trocados depois da leitura podem tornar a reprodução indisponível, com erro visível.

O estado utiliza os metadados locais. Sem confirmação conhecida de entrega, a reunião fica em Pendentes com indicação explícita, mesmo havendo notas. Fatos aguardando proveniência também mantêm pendência. Recibos antigos externos aos metadados não são reinterpretados pela biblioteca.

## Verificação

- `python3 -m unittest tests.test_library -q`: fixtures com HOME/XDG isolados, CLI real, bloqueio de caminhos externos, proveniência, estado de entrega e janela Quickshell real. Exige Wayland, Quickshell e QtMultimedia instalados, sem rede.
- `QT_QPA_PLATFORM=offscreen /usr/lib/qt6/bin/qmltestrunner -input tests/qml/tst_library.qml -o -,txt`: filtros, tempos, URLs locais e texto literal.

Risco AMARELO: nova interface e nova consulta local. Reversão: restaurar a revisão anterior pelo deployment do projeto; nenhum dado de reunião é migrado.
