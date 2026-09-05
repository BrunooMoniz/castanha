# Voz por origem, preparação F5V2

Risco VERMELHO na operação: áudio e transcrições pessoais. A flag
`transcription.por_canal` continua `false`. Esta entrega é local, com áudio
sintético, sem ativação, instalação no XPS, reprocessamento de acervo ou push público.

## Caminhos e origem

A flag cobre `process_pending` e `reprocess_meeting` (inclusive o caminho legado).
A separação usa FLAC mono derivado, mantendo os bytes do original e o PCM de cada
canal. Em captura `dual`, canal 0 é microfone e canal 1 é sistema. Em `mic_only`
(também aceito o nome legado `mic-only`), estéreo recebe a origem genérica
`gravacao_microfone`, com rótulo numerado por canal, sem alegar sistema ou pessoa
remota. Mono recebe `gravacao_mono`. Modo desconhecido é recusado.

Origem técnica não identifica pessoa: o microfone pode ter várias pessoas e eco;
o sistema pode incluir várias vozes e sons de outros aplicativos. O nome vindo
do provedor é substituído pelo rótulo técnico. Diarização e reconhecimento de
pessoa não foram implementados.

A VPS permanece pendente: o transporte remoto atual não cobre o FLAC por canal.
O original fica no Bronze. Bridge, adapter, sync, deployment e storage não fazem
parte desta mudança.

## Validação comum e retomada

O checkpoint versão 4 fica em `<bronze>/<slug>/.channels/<hash>/`, sob o lock da
reunião. Resposta ao vivo e checkpoint passam por `validate_channel_result` antes
de gravar ou usar: texto não vazio, tipos e contagens, provedor não simulado nem
pendente, identidade, canal, origem, rótulo, hashes e tempos finitos. Os tempos
precisam satisfazer `0 <= start <= end <= duração + 0,5 s`. Timestamps absolutos
no lugar de deslocamentos relativos são recusados.

O texto completo do provedor deve coincidir com a concatenação dos segmentos,
normalizando somente espaços. Um marcador que exista apenas em `result.text`
causa recusa explícita antes de checkpoint/Silver, sem descartar conteúdo em
silêncio. Isso pode recusar diferenças de pontuação legítimas: é uma política
conservadora, a ser tratada sem afrouxar a cobertura do conteúdo.

Checkpoint JSON malformado ou semanticamente inválido é preservado e recusado,
sem nova chamada de transcrição. Checkpoints antigos não são migrados nem
apagados. Uma falha por canal bloqueia a nova geração de Silver/Gold e ingestão
no caminho de jobs. No caminho legado, preserva-se a transcrição anterior.

O fingerprint canônico versão 1 inclui pipeline, provedor efetivo selecionado,
provedor configurado, modelos, idioma, modo de transcrição, modo de captura e
política/rótulo/origem. A serialização ordena as chaves e não inclui chaves de API
ou tokens. Mudança desses campos invalida o replay; reordenar chaves não invalida.
A seleção do provedor ocorre antes do replay, sem enviar áudio.

Canal medido como silencioso não é transcrito: registra provedor de silêncio,
texto vazio e zero segmentos. O replay confere a consistência e repete a medição.
Medição indisponível não presume silêncio. Dois canais mudos não produzem sucesso
vazio. Cada envio consulta orçamento por canal e recusa duração acima de 3 h.

## Várias gravações

O Bronze mantém canal, origem, hashes e tempos relativos por gravação.
`recorded_at` inválido é recusado antes de alterar os jobs ou gerar notas. Jobs
são ordenados pelo instante absoluto, considerando offset; datas legadas sem
fuso são tratadas como UTC para ordenação, sem alterar o valor armazenado.
Deslocamentos de fala continuam relativos ao início de cada gravação.

A agregação considera todos os jobs: fala real anterior vence silêncio posterior,
inclusive se a medição anterior era desconhecida. Erro ou pendência anterior
permanece em `processing_status` e `transcription_error`. Silêncio, mock e falha
não entram como gravação de memória real.

## Barreira no enriquecimento

Os prompts mantêm a instrução de separar origem e identidade. Há também uma
barreira determinística após a resposta: Silver retém o resumo gerado que
menciona pessoas da agenda ou termos de atribuição/presença reconhecidos pelo
filtro e anexa a transcrição integral. Gold remove itens correspondentes de
facts, decisions, action_items e people_notes.

A política é conservadora: até uma menção legítima à pessoa da agenda pode ser
retida. Nome citado, RSVP, convite ou inferência não viram prova de voz. No
frontmatter, convite e RSVP têm campos separados de `presence: unverified` e
`speech: unverified`. Não há mecanismo de comprovação de identidade pessoal nesta
fatia. O filtro cobre aliases de nomes/emails e termos testados; não é garantia
universal contra todas as paráfrases ou pessoas inventadas por um LLM.

## Provas e limites de ativação

Testes em `test_channel_transcription`, `test_durable_jobs`, `test_transcription`,
`test_summarizer` e os dois contratos cobrem separação PCM real com FFmpeg,
retomada sem recobrança, silêncio, orçamento, teto, original intacto, corrupção
semântica, cobertura integral do texto, fingerprint, modo, timestamps, múltiplos
jobs e LLM simulado tentando atribuir presença a convidado ausente do áudio.
O relatório F5V2 registra as contagens e o resultado efetivo da revisão.

Antes de ativar: ponte F4 com linhagem e contrato servidor, transporte VPS por
canal, QA real no XPS e mecanismo health-gated. Nada disso foi ativado aqui.

Reversão antes de operar: manter a flag desligada e a versão anterior disponível;
restaurar a versão pelo mecanismo health-gated quando houver instalação, preservando
Bronze, áudios, jobs e checkpoints. Não drenar checkpoint v4 com versão antiga,
não apagar derivados, não substituir o acervo pessoal nesta etapa.
