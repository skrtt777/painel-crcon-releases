# Painel CRCON v1.1.3

## Destaques

- Nova aba **Mensagens > Estatística** com:
  - Pré-visualização do formato enviado ao jogo
  - Log persistente de envios
  - Modo teste manual pelo painel
- Mensagem de estatísticas reformulada para leitura clara no jogo (formato em bloco, com quebras de linha)
- Envio de estatísticas na virada de mapa com retry automático quando o scoreboard final ainda não está disponível

## Novidades

- Adicionado botão **🧪 Modo Teste** para disparar a mensagem de estatística imediatamente no servidor
- Log de envio agora diferencia origem:
  - `auto` (envio automático)
  - `test` (envio manual de teste)
- Preview e renderização de categorias de ranking mais legíveis:
  - Abates
  - Sequência de Abates
  - Combate
  - Suporte
  - Ofensiva
  - Defesa

## Correções

- Corrigido caso onde o log de estatística não aparecia após virada de mapa quando o sistema de anúncios estava desativado
- Reduzidos travamentos na interface com ajustes de sincronização e cache
- Dashboard com atualização mais resiliente a timeouts de endpoint

## Observações importantes

- A API do CRCON não expõe quais jogadores confirmaram a mensagem com **Y**. O painel mostra log de envio, não confirmação individual.

## Arquivos desta versão

- `Painel_CRCON_v1.1.3.zip`
- `Painel_CRCON_v1.1.3.rar`

## Auto-update

- Versão: **1.1.3**
- Build date: **2026-03-08**
- Download URL:
  - https://raw.githubusercontent.com/skrtt777/painel-crcon-releases/main/releases/Painel_CRCON_v1.1.3.zip
