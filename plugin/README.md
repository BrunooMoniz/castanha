# Plugin do Omarchy

Um ícone na barra e um painel. `Panel.qml` é tudo: o botão da barra e o popup.

## O que aparece

**Barra.** Microfone quando ocioso. Gravando, o glifo de gravação do próprio
Omarchy (󰻂) mais o cronômetro, na cor `urgent` do tema. Com reunião no
calendário e nada gravando, o horário e o título dela. Clique abre o painel,
botão direito começa ou termina a gravação.

**Painel.** Um herói com o estado, o botão principal (iniciar / finalizar e
salvar), as ações secundárias (pausar, entrar na chamada, abrir as notas), a
reunião atual ou a próxima, e a última reunião com o que deu errado nela:
áudio mudo, erro na ingestão do Zinom.

**Aviso de microfone mudo.** O painel lê o mute da fonte padrão pelo PipeWire
e avisa antes, não depois. Foi assim que se perderam 35 segundos em 04/09/2026.

## Tema

Nenhuma cor literal no arquivo. Tudo vem de `Color.*` e `Style.*`, que o
omarchy-shell re-avalia quando o tema troca: `bar.foreground`, `bar.urgent`,
`Color.popups.*`, `Style.font.*`, `Style.space()`. Um `#ff5555` aqui vira
mancha no `catppuccin-latte` e some no `vantablack`.

Verificado com foto em `ethereal`, `catppuccin-latte`, `tokyo-night` e
`white`. No `white`, que é monocromático, o indicador de gravação sai em
cinza, porque é essa a cor de `urgent` lá. É o comportamento certo.

Os glifos são Material Design do Nerd Font, os mesmos que o resto do shell
usa, e todos conferidos no `JetBrainsMonoNerdFont-Regular.ttf` antes de
entrarem.

## Estado

O plugin não executa nada por conta própria: lê
`~/.local/state/castanha/state.json` e chama a CLI `castanha`. O cronômetro
sai do `started_at`, não do `elapsed_seconds`, porque aquele campo só anda
quando o daemon está de pé.

## IPC

```
omarchy-shell castanha open|close|toggle
```

## Instalar

`./install.sh` na raiz do projeto cria o symlink em
`~/.config/omarchy/plugins/moniz.castanha`. Depois:

```
omarchy restart shell
```

`rescanPlugins` recarrega o arquivo mas mantém o componente QML em cache;
para mudança de verdade, reinicie o shell.

## Controles no painel

**Não mostrar um evento.** Passar o mouse numa reunião mostra o botão 󰈉. Ele
esconde a **série inteira**, não a ocorrência de hoje: lembrete semanal se
esconde uma vez. Volta com `castanha agenda unhide <chave>`, e
`castanha agenda hidden` lista o que está escondido. A marca fica em
`~/.local/state/castanha/hidden_events.json`, não na config, porque quem
escreve ali é o clique.

**Sincronizar de novo com o Zinom.** A ingestão roda uma vez, no fim da
gravação. Se o hub estava fora do ar, a reunião ficava só em disco. Agora a
linha da nota mostra o botão 󰑐 quando o envio falhou, e
`castanha sync [slug]` (ou `--all`) faz o mesmo pela CLI. Reenviar é seguro:
a nota é EDITADA pelo id gravado no metadata do Bronze, e `brain_fact`
supersede o fato do mesmo par sujeito-predicado. Rodar duas vezes não cria
duas notas.
