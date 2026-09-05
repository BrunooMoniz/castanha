# Voz por origem, preparação F5V

Risco VERMELHO na ativação: áudio e transcrições pessoais. Não instalado nem
conectado ao daemon nesta entrega. Nenhuma reunião pessoal foi reprocessada.

O original dual já contém microfone no canal 0 e sistema no canal 1. Enviar
o estéreo inteiro ao Whisper da Groq perde essa distinção (a API converte
para mono). O novo módulo gera FLAC mono por canal sem outra compressão com
perda, conserva o original e recompõe segmentos pelo tempo, inclusive sobreposições.

Os rótulos são origens técnicas, não identidade biométrica. Microfone pode
conter mais de uma pessoa na sala ou vazamento dos alto-falantes. Sistema
inclui notificações e todos os participantes online juntos. Não atribuir nome
do calendário a um falante sem evidência. Separar pessoas remotas exige diarização
adicional, não entregue por este módulo.

Prova automatizada: seis testes com áudio sintético real via FFmpeg, incluindo
igualdade PCM entre cada canal original e FLAC, original intacto, ordenação
com sobreposição, retomada após falha do segundo canal sem repetir o primeiro,
rejeição de checkpoint corrompido e timestamps inválidos. Nenhum provedor real chamado.

Antes de ativar, ainda é obrigatório:

- Integrar callback mono com Groq/VPS, MIME FLAC e orçamento por canal. Dois
  canais podem representar até o dobro dos minutos faturados. Não ignorar teto.
- Tratar canal efetivamente silencioso sem gerar fala inventada. O módulo atual
  recusa resposta vazia, portanto ainda não cobre gravação com microfone mudo.
- Verificar ponta a ponta os segmentos com origem e ID da gravação no engine.
  A persistência já foi implementada em transcript_segments.json e no checkpoint,
  com prova de retomada após falha do resumo sem nova transcrição. Os quatro
  prompts de enriquecimento já recebem a regra de canal versus identidade;
  isso não é prova comportamental de que um modelo real sempre a respeitará.
- Exercitar retomada remota, mistura de canais concluídos e pendentes, e ponte
  integral F4. O chamador deve manter meeting_lock durante a operação.
- Rever checkpoints incompatíveis ao trocar modelo/versão, sem apagar histórico.
- Revisão independente Claude, QA real e instalação reversível conforme F5P.

Reversão: manter versão anterior e áudio original; antes de ativar registrar
os alvos CLI/plugin e estado do daemon conforme INSTALACAO-REVERSIVEL.md. Não
apagar derivados nem checkpoints durante rollback. A nova versão não deve
substituir a transcrição de Nora até completar ambas as origens e a verificação.

Referência: https://console.groq.com/docs/speech-to-text (consultada em 05/09/2026).
