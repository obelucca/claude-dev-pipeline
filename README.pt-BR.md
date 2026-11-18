# dev-pipeline para o Claude Code

[Read in English](README.md)

Um plugin do Claude Code com duas skills e seis agentes por papel. Ele saiu do dia a dia de um
monorepo com vários serviços (Java, Go, React Native, React), que várias pessoas e várias sessões
do Claude usam ao mesmo tempo.

- **`/review-pr`** revisa um PR ou uma branch. Um script roda em segundos, sem gastar token, tudo
  o que uma regra decide. Um agente revisor em contexto limpo julga o resto. O resultado é um
  relatório único, que pode virar um comentário no PR, com até duas rodadas de correção automática.
- **`/parallel-dev`** desenvolve as tarefas de uma frente em ondas, com um worktree, uma branch,
  um commit e um PR por tarefa. Cada tarefa é implementada por um subagente e revisada por outro
  que não a viu sendo escrita.

## Por quê

O desenho vem de duas lições:

1. **O que uma regra decide não deve custar modelo.** O `check.py` decide a numeração de
   migration, a base do PR, o conflito, o segredo e a chamada proibida a uma API. O modelo só
   recebe o que pede julgamento, e já com a saída do script, para não redescobrir nada.
2. **Revisor só serve se não estiver ancorado.** Ele nunca vê o raciocínio do autor nem o seu: só
   o diff e o contrato. Cada finding é conferido no código antes de entrar no relatório, porque o
   revisor erra no mesmo tom confiante em que acerta.

### Os incidentes que motivaram as checagens

Todos mesclaram limpo no git e só falharam em tempo de execução:

| checagem | o que aconteceu |
|---|---|
| `migration-number` | versão livre no `main` mas já aplicada no `develop`: *"Detected resolved migration not applied to database"* |
| `migration-race` | dois PRs abertos escolheram a mesma versão na mesma tarde; os dois mesclaram e o boot morreu em *"Found more than one migration with version N"* |
| `migration-edited` | um comentário no cabeçalho de uma migration já aplicada mudou o checksum |
| `pr-base` | um PR empilhado numa branch que já tinha mesclado foi mesclado também, só que numa branch morta, e nunca chegou ao `main` |
| `read-only-api` | uma integração que, em produção, só pode ler pedidos e pagamentos |

## Instalar

Como plugin (recomendado):

```
/plugin marketplace add obelucca/claude-dev-pipeline
/plugin install dev-pipeline@obelucca
```

Ou copie `skills/*` para `<repo>/.claude/skills/` (ou `~/.claude/skills/`) e `agents/*` para
`.claude/agents/`.

Requisitos: `git` 2.38+, Python 3.11+ e o GitHub CLI (`gh`), para os dados e os comentários do PR.

## Configurar

Copie o [`skills/review-pr/review-pr.example.toml`](skills/review-pr/review-pr.example.toml) para
`<repo>/.claude/review-pr.toml` e fique com o que se aplica. Tudo é opcional. Sem o arquivo, ainda
rodam as checagens de nome, base do PR, conflito e segredo.

- `compare_branches`: outras linhas longas cujas migrations também contam para o teto.
- `[[migrations]]`: `kind = "flyway"`, `"golang-migrate"` ou uma regex sua.
- `[[read_only_apis]]`: um recurso de API que nunca pode aparecer ao lado de um verbo de escrita.
- `[[path_flags]]`: caminhos que geram nota, aviso ou bloqueio (telas a comparar com o design,
  arquivos de CI etc.).
- `[[tests]]`: como testar só o que mudou, com `maven`, `go`, `node` (jest/vitest) ou `command`.
- `[reviewer]`: contexto do projeto, regras do repositório e a referência de design para o prompt
  do revisor.

## Usar

```
/review-pr                 # branch atual
/review-pr 123 --comment   # PR #123, com o relatório em um comentário
/review-pr --fix           # branch sua: corrige bloqueios e findings conferidos, até 2 rodadas
/parallel-dev              # planeja as ondas a partir das tarefas abertas de uma frente
```

O script também roda sozinho:

```bash
python skills/review-pr/scripts/check.py --pr 123
python skills/review-pr/scripts/check.py --dir ../worktree --run-tests --json saida.json
```

O código de saída é 0 sem bloqueio, 2 com pelo menos um bloqueio e 1 para erro de uso.

## Agentes

| agente | modelo | papel |
|---|---|---|
| `pipeline-architect` | Opus, high | planeja antes do código; só lê |
| `pipeline-dev` | Sonnet, medium | trabalho leve ou bem especificado |
| `pipeline-dev-heavy` | Opus, medium | dinheiro, pagamento, autenticação, migration |
| `pipeline-reviewer` | Opus, high | revisão em contexto limpo; só lê |
| `pipeline-qa-checklist` | Haiku, medium | lista fechada de critérios objetivos |
| `pipeline-qa-flow` | Sonnet, high | ponta a ponta na pilha local, procurando o caso que quebra |

## Desenvolvimento

```bash
python -m unittest discover -s tests -v
```

Os testes montam repositórios git descartáveis e conferem cada checagem de ponta a ponta.

## Licença

[MIT](LICENSE)
