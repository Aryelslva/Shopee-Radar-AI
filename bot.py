import os
import json
import time
import hashlib
import logging
import asyncio
import html
import re
import urllib.request
import urllib.error
import threading
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

# Telegram e Shopee usam credenciais DIFERENTES.
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SHOPEE_APP_ID = os.getenv("SHOPEE_APP_ID")
SHOPEE_SECRET = os.getenv("SHOPEE_SECRET")

# Configuração do Asaas. Nesta etapa, o endpoint apenas recebe e valida
# notificações; ainda não cria cobranças nem libera assinaturas.
ASAAS_API_KEY = os.getenv("ASAAS_API_KEY")
ASAAS_ENV = os.getenv("ASAAS_ENV", "sandbox").strip().lower()
ASAAS_WEBHOOK_TOKEN = os.getenv("ASAAS_WEBHOOK_TOKEN", "")

SHOPEE_API_URL = "https://open-api.affiliate.shopee.com.br/graphql"

# Controle persistente do benefício gratuito (uma pesquisa por ID do Telegram).
# Em produção, configure RADAR_DB_PATH para um caminho dentro do volume persistente
# do Railway. Sem volume persistente, os dados podem ser perdidos em um redeploy.
import sqlite3

RADAR_DB_PATH = os.getenv("RADAR_DB_PATH", "radar_access.db")


def iniciar_banco():
    with sqlite3.connect(RADAR_DB_PATH) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS usuarios (
                telegram_id INTEGER PRIMARY KEY,
                pesquisa_gratis_usada INTEGER NOT NULL DEFAULT 0
            )
        """)
        db.commit()


def pesquisa_gratis_disponivel(telegram_id: int) -> bool:
    with sqlite3.connect(RADAR_DB_PATH) as db:
        db.execute(
            "INSERT OR IGNORE INTO usuarios (telegram_id, pesquisa_gratis_usada) VALUES (?, 0)",
            (telegram_id,),
        )
        linha = db.execute(
            "SELECT pesquisa_gratis_usada FROM usuarios WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        return linha is not None and linha[0] == 0


def consumir_pesquisa_gratis(telegram_id: int) -> bool:
    # A atualização condicional evita que duas pesquisas simultâneas consumam
    # duas vezes o benefício ou liberem duas pesquisas gratuitas.
    with sqlite3.connect(RADAR_DB_PATH) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT OR IGNORE INTO usuarios (telegram_id, pesquisa_gratis_usada) VALUES (?, 0)",
            (telegram_id,),
        )
        cursor = db.execute(
            "UPDATE usuarios SET pesquisa_gratis_usada = 1 "
            "WHERE telegram_id = ? AND pesquisa_gratis_usada = 0",
            (telegram_id,),
        )
        db.commit()
        return cursor.rowcount == 1


async def bloquear_sem_acesso(mensagem):
    await mensagem.reply_text(
        "🔒 Sua pesquisa gratuita já foi utilizada. "
        "Escolha um plano para continuar usando o Shopee Radar AI.\n\n"
        "📅 Mensal: R$ 19,90\n"
        "📅 Trimestral: R$ 54,90\n"
        "📅 Semestral: R$ 99,90\n"
        "📅 Anual: R$ 179,90\n\n"
        "A liberação automática pelo Asaas será ativada na etapa de integração dos pagamentos."
    )



def numero(valor):
    """Converte um valor numérico; retorna None se estiver ausente/inválido."""
    if valor is None or valor == "":
        return None
    try:
        if isinstance(valor, str):
            valor = valor.strip().replace(",", ".")
        return float(valor)
    except (TypeError, ValueError):
        return None


def dinheiro(valor):
    """Formata número no padrão brasileiro."""
    valor = numero(valor)
    if valor is None:
        return "Não informado"
    return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def buscar_produtos_shopee(termo: str, pagina: int = 1) -> dict:
    """Consulta uma página de ofertas na Open API de Afiliados Shopee."""
    if not SHOPEE_APP_ID or not SHOPEE_SECRET:
        raise RuntimeError("Credenciais da Shopee ausentes. Configure SHOPEE_APP_ID e SHOPEE_SECRET.")

    termo_graphql = json.dumps(termo, ensure_ascii=False)
    query = (
        "{ productOfferV2("
        f"keyword: {termo_graphql}, sortType: 5, page: {pagina}, limit: 10"
        ") { nodes { itemId productName commissionRate price priceMin priceMax "
        "imageUrl offerLink shopName ratingStar } pageInfo { page limit hasNextPage } } }"
    )
    body = json.dumps({"query": query}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    timestamp = str(int(time.time()))
    assinatura_base = SHOPEE_APP_ID + timestamp + body.decode("utf-8") + SHOPEE_SECRET
    signature = hashlib.sha256(assinatura_base.encode("utf-8")).hexdigest()
    authorization = f"SHA256 Credential={SHOPEE_APP_ID}, Timestamp={timestamp}, Signature={signature}"
    request = urllib.request.Request(
        SHOPEE_API_URL, data=body,
        headers={"Authorization": authorization, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detalhe = exc.read().decode("utf-8", errors="replace")
        logging.error("Shopee HTTP %s: %s", exc.code, detalhe[:1500])
        raise RuntimeError(f"A Shopee retornou HTTP {exc.code}. Confira as credenciais e tente novamente.") from exc
    except urllib.error.URLError as exc:
        logging.error("Falha de conexão com Shopee: %s", exc)
        raise RuntimeError("Não foi possível conectar à Shopee. Confira a internet e tente novamente.") from exc
    if result.get("errors"):
        logging.error("Erro GraphQL Shopee: %s", result["errors"])
        mensagens = "; ".join(str(item.get("message", "Erro GraphQL")) for item in result["errors"])
        raise RuntimeError(f"A API da Shopee recusou a consulta: {mensagens}")
    oferta = result.get("data", {}).get("productOfferV2", {}) or {}
    return {"nodes": oferta.get("nodes", []) or [], "pageInfo": oferta.get("pageInfo", {}) or {}}


def calcular_pontuacoes(produtos):
    """
    Pontuação PARCIAL de até 45 pontos:
    - Comissão: até 35 pontos, relativa à maior comissão da pesquisa.
    - Avaliação: até 10 pontos, na escala de 0 a 5 estrelas.

    Não representa a pontuação completa de oportunidade.
    """
    comissoes = [
        numero(p.get("commissionRate"))
        for p in produtos
        if numero(p.get("commissionRate")) is not None
        and numero(p.get("commissionRate")) > 0
    ]
    maior_comissao = max(comissoes) if comissoes else None

    resultados = []
    for produto in produtos:
        comissao = numero(produto.get("commissionRate"))
        avaliacao = numero(produto.get("ratingStar"))

        pontos_comissao = 0.0
        pontos_avaliacao = 0.0

        if comissao is not None and comissao > 0 and maior_comissao:
            pontos_comissao = (comissao / maior_comissao) * 35

        if avaliacao is not None:
            pontos_avaliacao = (
                max(0.0, min(avaliacao, 5.0)) / 5.0
            ) * 10

        item = dict(produto)
        item["_pontos_comissao"] = round(pontos_comissao, 1)
        item["_pontos_avaliacao"] = round(pontos_avaliacao, 1)
        item["_pontuacao"] = round(
            pontos_comissao + pontos_avaliacao, 1
        )
        item["_comissao_disponivel"] = comissao is not None
        item["_avaliacao_disponivel"] = avaliacao is not None
        resultados.append(item)

    resultados.sort(
        key=lambda p: p["_pontuacao"],
        reverse=True,
    )
    return resultados


def pontuar_relevancia(produto: dict, termo: str) -> int:
    """Ordena por correspondência textual, sem descartar automaticamente resultados."""
    nome = str(produto.get("productName") or "").lower()
    palavras = [p for p in re.findall(r"\w+", termo.lower()) if len(p) > 2]
    return (20 if termo.lower().strip() in nome else 0) + sum(1 for p in palavras if p in nome)


def formatar_legenda(produto: dict, indice: int) -> str:
    """Monta legenda HTML curta para acompanhar a foto no Telegram."""
    nome = html.escape(
        str(produto.get("productName") or "Produto sem nome")
    )
    loja = html.escape(
        str(produto.get("shopName") or "Loja não informada")
    )

    preco = (
        produto.get("priceMin")
        or produto.get("price")
        or produto.get("priceMax")
    )
    comissao = numero(produto.get("commissionRate"))
    avaliacao = numero(produto.get("ratingStar"))

    pontuacao = produto.get("_pontuacao", 0)
    pontos_comissao = produto.get("_pontos_comissao", 0)
    pontos_avaliacao = produto.get("_pontos_avaliacao", 0)

    linhas = [
        f"🛍️ <b>#{indice} | {nome}</b>",
        f"🏪 Loja: {loja}",
        f"💵 <b>Preço: {dinheiro(preco)}</b>",
    ]

    if comissao is not None:
        # Mantém a mesma convenção de exibição usada na versão anterior.
        linhas.append(
            f"💰 <b>Comissão informada: {comissao * 100:.2f}%</b>"
        )
    else:
        linhas.append("💰 Comissão: não informada")

    if avaliacao is not None:
        estrelas = max(0, min(5, int(round(avaliacao))))
        linhas.append(
            f"⭐ Avaliação: {'★' * estrelas}{'☆' * (5 - estrelas)} "
            f"({avaliacao:.2f}/5)"
        )
    else:
        linhas.append("⭐ Avaliação: não informada")

    linhas.extend([
        "",
        f"🎯 <b>Radar AI: {pontuacao:.1f}/45 pontos</b>",
        f"   Comissão: {pontos_comissao:.1f}/35",
        f"   Avaliação: {pontos_avaliacao:.1f}/10",
        "",
        "ℹ️ <i>Pontuação parcial: ainda não considera "
        "concorrência, vendas/GMV ou potencial de conteúdo.</i>",
    ])

    link = produto.get("offerLink") or produto.get("productLink")
    if link:
        link_seguro = html.escape(str(link), quote=True)
        linhas.extend([
            "",
            f'🔗 <a href="{link_seguro}">Abrir oferta de afiliado</a>',
        ])

    legenda = "\n".join(linhas)

    # O Telegram limita a legenda de foto a 1024 caracteres.
    if len(legenda) > 1000:
        legenda = legenda[:990] + "…"
    return legenda


def formatar_texto_sem_imagem(produto: dict, indice: int) -> str:
    """Alternativa quando a foto não existe ou o Telegram não consegue carregá-la."""
    legenda = formatar_legenda(produto, indice)
    # Remove tags HTML para a mensagem de texto continuar legível.
    texto = legenda.replace("<b>", "").replace("</b>", "")
    texto = texto.replace("<i>", "").replace("</i>", "")
    texto = texto.replace("</a>", "")
    import re
    texto = re.sub(r'<a href="[^"]+">', "", texto)
    return texto

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🛰️ SHOPEE RADAR AI\n\n"
        "Encontre ofertas de afiliados e acompanhe a pontuação dos produtos.\n\n"
        "🔎 Pesquisar: /buscar ferramentas\n"
        "🏆 Ranking: /top ferramentas\n"
        "📊 Analisar: use o botão em um produto ou /analisar seguido do link.\n"
        "Use /ajuda para consultar os comandos.",
        parse_mode=ParseMode.HTML,
    )



async def enviar_resultados(update, context, produtos, pagina, tem_mais):
    chat = (
        getattr(update, "effective_message", None)
        or getattr(update, "message", None)
    )

    if chat is None:
        return

    await chat.reply_text(
        f"🛰️ <b>SHOPEE RADAR AI</b> — página {pagina}\n"
        "Resultados ordenados por relevância textual e pontuação parcial.\n"
        "A nota considera comissão e avaliação; ainda não mede concorrência nem vendas.",
        parse_mode=ParseMode.HTML,
    )

    cache = context.user_data.setdefault("radar_produtos", {})

    for indice, produto in enumerate(produtos, start=1):
        chave = f"{pagina}_{indice}"
        cache[chave] = produto

        teclado = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "📊 Analisar este produto",
                callback_data=f"analisar:{chave}"
            )
        ]])

        legenda = formatar_legenda(produto, indice)
        imagem = produto.get("imageUrl")

        if imagem:
            try:
                await chat.reply_photo(
                    photo=imagem,
                    caption=legenda,
                    parse_mode=ParseMode.HTML,
                    reply_markup=teclado,
                )
                continue
            except Exception as exc:
                logging.warning(
                    "Falha ao enviar imagem do produto %s: %s",
                    indice,
                    exc,
                )

        await chat.reply_text(
            formatar_texto_sem_imagem(produto, indice),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=teclado,
        )

    context.user_data["radar_tem_mais"] = bool(tem_mais)

    if tem_mais:
        await chat.reply_text(
            "👇 Quer ver outros produtos desta pesquisa?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "➕ Mais produtos",
                    callback_data=f"mais:{pagina + 1}"
                )
            ]]),
        )
    else:
        await chat.reply_text(
            "✅ Não há mais páginas disponíveis para esta pesquisa."
        )



async def executar_busca(update, context, termo, pagina=1):
    context.user_data["radar_termo_atual"] = termo
    chat = getattr(update, "effective_message", None) or getattr(update, "message", None)
    if chat is None:
        return
    await chat.reply_text(
        f"🔎 Pesquisando <b>{html.escape(termo)}</b> — página {pagina}…",
        parse_mode=ParseMode.HTML,
    )
    try:
        resposta = await asyncio.to_thread(buscar_produtos_shopee, termo, pagina)
        produtos = resposta["nodes"]
        if not produtos:
            await chat.reply_text("Não encontrei mais ofertas para esse termo. Tente outra palavra.")
            return
        produtos = calcular_pontuacoes(produtos)
        produtos.sort(
            key=lambda p: (pontuar_relevancia(p, termo), p.get("_pontuacao", 0)),
            reverse=True,
        )
        if pagina == 1 and context.user_data.get("radar_busca_gratuita_pendente"):
            user_id = update.effective_user.id
            if not consumir_pesquisa_gratis(user_id):
                await bloquear_sem_acesso(chat)
                context.user_data["radar_busca_gratuita_pendente"] = False
                return
            context.user_data["radar_busca_gratuita_pendente"] = False
            context.user_data["radar_busca_gratuita_em_andamento"] = True
        await enviar_resultados(update, context, produtos, pagina,
                                bool(resposta.get("pageInfo", {}).get("hasNextPage", False)))
    except Exception:
        logging.exception("Falha ao pesquisar produtos na Shopee")
        await chat.reply_text(
            "⚠️ Não consegui concluir a pesquisa na Shopee. O detalhe técnico ficou registrado no CMD."
        )


async def buscar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    termo = " ".join(context.args).strip()
    if not termo:
        await update.effective_message.reply_text("🔎 Digite o produto que deseja pesquisar.\n\nExemplo: /buscar ferramentas")
        return
    user_id = update.effective_user.id
    if not pesquisa_gratis_disponivel(user_id):
        await bloquear_sem_acesso(update.effective_message)
        return
    context.user_data["radar_busca_gratuita_pendente"] = True
    await executar_busca(update, context, termo, 1)


async def enviar_analise_produto(mensagem, produto):
    nome = html.escape(str(produto.get("productName") or "Produto sem nome"))
    loja = html.escape(str(produto.get("shopName") or "Loja não informada"))
    preco = produto.get("priceMin") or produto.get("price") or produto.get("priceMax")
    comissao = numero(produto.get("commissionRate"))
    avaliacao = numero(produto.get("ratingStar"))
    linhas = [
        "📊 <b>ANÁLISE DO PRODUTO</b>", f"🛍️ <b>{nome}</b>", f"🏪 Loja: {loja}",
        f"💵 Preço informado: {dinheiro(preco)}",
        f"⭐ Avaliação: {avaliacao:.2f}/5" if avaliacao is not None else "⭐ Avaliação: não informada",
        f"💰 Comissão informada pela API: {comissao * 100:.2f}%" if comissao is not None else "💰 Comissão: não informada",
        "", "<b>O que os dados permitem concluir</b>",
        "• Esta consulta informa preço, comissão e avaliação quando disponíveis.",
        "• Vendas, GMV e quantidade de afiliados não estão confirmados nesta consulta.",
        "• Confirme a comissão efetiva na sua conta de afiliado antes de divulgar o produto.",
    ]
    link = produto.get("offerLink") or produto.get("productLink")
    if link:
        linhas.extend(["", f'<a href="{html.escape(str(link), quote=True)}">Abrir oferta de afiliado</a>'])
    await mensagem.reply_text("\n".join(linhas), parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def botoes_radar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    dados = query.data or ""
    if dados.startswith("mais:"):
        pagina = int(dados.split(":", 1)[1])
        termo = context.user_data.get("radar_termo_atual")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        if termo and context.user_data.get("radar_busca_gratuita_em_andamento"):
            await executar_busca(query, context, termo, pagina)
        elif termo:
            await query.message.reply_text(
                "🔒 Para iniciar outra pesquisa, escolha um dos planos do Shopee Radar AI."
            )
        return
    if dados.startswith("analisar:"):
        chave = dados.split(":", 1)[1]
        produto = context.user_data.get("radar_produtos", {}).get(chave)
        if produto:
            await enviar_analise_produto(query.message, produto)
        else:
            await query.message.reply_text("Esse produto não está mais na sessão. Faça a pesquisa novamente.")


async def analisar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    link = " ".join(context.args).strip()
    produtos = context.user_data.get("radar_produtos", {})
    if link:
        produto = next((p for p in produtos.values()
                        if link == str(p.get("offerLink") or p.get("productLink") or "")), None)
        if produto:
            await enviar_analise_produto(update.effective_message, produto)
        else:
            await update.effective_message.reply_text(
                "Recebi o link, mas a API atual não confirmou uma consulta direta por URL. "
                "Use /buscar com o nome do produto e toque em ‘Analisar este produto’ no resultado."
            )
        return
    await update.effective_message.reply_text(
        "📊 Para analisar um produto, toque em <b>Analisar este produto</b> em um resultado de /buscar.\n"
        "Você também pode usar /analisar seguido do link de um produto que já apareceu na sua busca.",
        parse_mode=ParseMode.HTML,
    )


async def top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    termo = " ".join(context.args).strip()
    if not termo:
        await update.effective_message.reply_text(
            "🏆 Informe uma categoria para ver os melhores resultados da pesquisa.\nExemplo: /top ferramentas"
        )
        return
    user_id = update.effective_user.id
    if not pesquisa_gratis_disponivel(user_id):
        await bloquear_sem_acesso(update.effective_message)
        return
    context.user_data["radar_busca_gratuita_pendente"] = True
    await executar_busca(update, context, termo, 1)


async def ajuda(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    await start(update, context)



class AsaasWebhookHandler(BaseHTTPRequestHandler):
    """Endpoint inicial do webhook Asaas; não altera acesso nem pagamentos."""

    def do_GET(self):
        if self.path.rstrip("/") == "/health":
            self._responder(200, {"status": "ok", "service": "shopee-radar-ai"})
        else:
            self._responder(404, {"error": "not_found"})

    def do_POST(self):
        if self.path.rstrip("/") != "/webhooks/asaas":
            self._responder(404, {"error": "not_found"})
            return

        if not ASAAS_WEBHOOK_TOKEN:
            self._responder(503, {"error": "webhook_token_not_configured"})
            return

        token_recebido = self.headers.get("asaas-access-token", "")
        if not hmac.compare_digest(token_recebido, ASAAS_WEBHOOK_TOKEN):
            self._responder(401, {"error": "unauthorized"})
            return

        try:
            tamanho = int(self.headers.get("Content-Length", "0"))
            if tamanho <= 0 or tamanho > 1_000_000:
                self._responder(400, {"error": "invalid_content_length"})
                return
            payload = json.loads(self.rfile.read(tamanho).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("O corpo precisa ser um objeto JSON.")
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self._responder(400, {"error": "invalid_json"})
            return

        # Não registrar dados pessoais ou o conteúdo integral da cobrança.
        evento = str(payload.get("event", "unknown"))[:100]
        pagamento = payload.get("payment") or {}
        pagamento_id = str(pagamento.get("id", "unknown"))[:100] if isinstance(pagamento, dict) else "unknown"
        logging.info("Webhook Asaas recebido e autenticado: event=%s payment_id=%s", evento, pagamento_id)
        self._responder(200, {"received": True, "processed": False})

    def _responder(self, status, dados):
        corpo = json.dumps(dados).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def log_message(self, formato, *args):
        logging.info("HTTP webhook: " + formato, *args)


def iniciar_servidor_webhook():
    """Inicia o endpoint HTTP em paralelo ao polling do Telegram."""
    porta = int(os.getenv("PORT", "8080"))
    servidor = ThreadingHTTPServer(("0.0.0.0", porta), AsaasWebhookHandler)
    thread = threading.Thread(target=servidor.serve_forever, name="asaas-webhook", daemon=True)
    thread.start()
    logging.info("Servidor HTTP ativo na porta %s; endpoint POST /webhooks/asaas", porta)
    return servidor

def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError(
            "Token do Telegram não configurado. "
            "Configure TELEGRAM_BOT_TOKEN no CMD."
        )

    iniciar_banco()
    servidor_webhook = iniciar_servidor_webhook()
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("buscar", buscar))
    app.add_handler(CommandHandler("analisar", analisar))
    app.add_handler(CommandHandler("top", top))
    app.add_handler(CommandHandler("ajuda", ajuda))
    app.add_handler(CallbackQueryHandler(botoes_radar))

    print("SHOPEE RADAR AI INICIADO!")
    print(
        "Integração Shopee: credenciais configuradas."
        if SHOPEE_APP_ID and SHOPEE_SECRET
        else "ATENÇÃO: configure SHOPEE_APP_ID e SHOPEE_SECRET no CMD."
    )

    try:
        app.run_polling()
    finally:
        servidor_webhook.shutdown()
        servidor_webhook.server_close()


if __name__ == "__main__":
    main()
