
import os
import time
import queue
import threading
import requests
import pytchat
import shutil
import itertools
import random

from flask import Flask, jsonify, Response
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import TimeoutException


# ============================================================
# CONFIGURAÇÕES
# ============================================================

CANAL = "@jp1414"
LIVE_ID = None

# Quando nenhum ID/link é informado na inicialização, procura
# automaticamente a transmissão mais recente do canal.
INTERVALO_VERIFICACAO_LIVE = 20
TIMEOUT_BUSCA_LIVE = 15

FIREBASE_URL = (
    "https://botdo-6db23-default-rtdb.firebaseio.com/"
)

PERFIL = os.path.expanduser(
    "~/youtube-client/perfis/conta1"
)

CHROMIUM = (
    "/data/data/com.termux/files/usr/bin/chromium-browser"
)

CHROMEDRIVER = (
    "/data/data/com.termux/files/usr/bin/chromedriver"
)

VERSAO = "156.0.8078.79"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/156.0.0.0 Safari/537.36"
)

RECOMPENSA = 1
INTERVALO_RECOMPENSA = 30
BONUS_POR_MENSAGENS = 15
MENSAGENS_PARA_BONUS = 10
LIMITE_RECOMPENSA_HORA = 120

RECOMPENSA_DIARIA = 25
INTERVALO_DIARIO = 86400

INTERVALO_PESCA = 60
PRECO_BOOST_2X = 100
DURACAO_BOOST_2X = 600

PRECO_SORTE_PESCA = 200
DURACAO_SORTE_PESCA = 600

INTERVALO_COMANDO = 5
INTERVALO_SOM = 5

# GitHub
GITHUB_API = (
    "https://api.github.com/repos/"
    "joaop1414/SonsDoBot/contents"
)

INTERVALO_CACHE_SONS = 300

# Preços dos sons armazenados no Realtime Database
FIREBASE_SONS_URL = FIREBASE_URL + "sons.json"

EXTENSOES_AUDIO = (
    ".mp3",
    ".wav",
    ".ogg",
    ".m4a",
    ".aac",
    ".webm"
)

# Flask / OBS
FLASK_HOST = "0.0.0.0"
FLASK_PORTA = 5000

SELETOR_CAMPO = (
    "yt-live-chat-text-input-field-renderer"
    "#input div#input[contenteditable]"
)

# Tempos de espera
TIMEOUT_FIREBASE = 5
TIMEOUT_ENVIO = 3

rodando = True
driver = None

sessao = requests.Session()

fila_mensagens = queue.Queue()
fila_sons = queue.Queue()
fila_eventos_chat = queue.Queue()
fila_recompensas = queue.Queue()

jogadores = {}

ultimo_help = {}
ultimo_moedas = {}
ultimo_som = {}
ultimo_diario = {}
ultimo_perfil = {}
ultimo_top = {}
ultimo_pesca = {}
ultimo_loja = {}

sorteio_ativo = False
sorteio_premio = 0
sorteio_participantes = {}
lock_sorteio = threading.Lock()

lock_jogadores = threading.Lock()
lock_transacoes = threading.Lock()
lock_envio = threading.Lock()
lock_catalogo = threading.Lock()
lock_cooldown = threading.Lock()

contador_sons = itertools.count(1)

catalogo_cache = {
    "sons": {},
    "atualizado": 0
}


# ============================================================
# FIREBASE
# ============================================================

def firebase_url(channel_id):

    return (
        FIREBASE_URL
        + "jogadores/"
        + channel_id
        + ".json"
    )


def firebase_buscar_jogador(channel_id):

    try:

        resposta = sessao.get(
            firebase_url(channel_id),
            timeout=TIMEOUT_FIREBASE
        )

        resposta.raise_for_status()

        dados = resposta.json()

        # Um jogador inexistente retorna null no Firebase.
        # Nesse caso, consideramos um registro vazio.
        if dados is None:
            return {}

        if not isinstance(dados, dict):
            return {}

        return dados

    except Exception as erro:

        print("[FIREBASE] Erro ao buscar jogador:", erro)

        # None significa que a consulta falhou.
        return None


def firebase_atualizar_jogador(channel_id, dados):

    try:

        resposta = sessao.patch(
            firebase_url(channel_id),
            json=dados,
            timeout=TIMEOUT_FIREBASE
        )

        resposta.raise_for_status()

        return True

    except Exception as erro:

        print("[FIREBASE] Erro ao atualizar jogador:", erro)

        return False


# ============================================================
# JOGADORES E MOEDAS
# ============================================================

def obter_jogador(channel_id, nome):

    with lock_jogadores:

        if channel_id in jogadores:

            jogador = jogadores[channel_id]
            jogador["nome"] = nome

            return jogador

    # Consulta o Firebase quando o jogador ainda não está
    # carregado na memória.
    dados = firebase_buscar_jogador(channel_id)

    if dados is None:
        return None

    try:
        moedas = int(dados.get("moedas", 0))
    except (ValueError, TypeError):
        moedas = 0

    try:
        ultima_mensagem = int(
            dados.get("ultima_mensagem", 0)
        )
    except (ValueError, TypeError):
        ultima_mensagem = 0

    jogador = {
        "nome": nome,
        "channelId": channel_id,
        "moedas": moedas,
        "ultima_mensagem": ultima_mensagem
    }

    with lock_jogadores:

        if channel_id in jogadores:
            return jogadores[channel_id]

        jogadores[channel_id] = jogador

    return jogador


def processar_jogador(channel_id, nome, mensagem):
    """
    Recompensa participação no chat:
    - 1 moeda a cada 30 segundos com mensagens válidas.
    - 15 moedas extras a cada 10 mensagens válidas.
    - No máximo 120 moedas efetivamente creditadas por hora.
    - Mensagens iguais consecutivas e mensagens enviadas em menos de
      3 segundos da última mensagem contada não avançam o contador.
    """

    if not channel_id:
        return

    jogador = obter_jogador(channel_id, nome)

    if jogador is None:
        return

    agora = int(time.time())
    texto = (mensagem or "").strip()
    texto_normalizado = texto.casefold()

    # Comandos e mensagens muito curtas não contam como participação.
    mensagem_valida = (
        len(texto) >= 2
        and not texto.startswith("!")
    )

    with lock_transacoes:

        dados = firebase_buscar_jogador(channel_id)

        if dados is None:
            print(
                "[MOEDAS] Recompensa cancelada: "
                "não foi possível consultar o Firebase."
            )
            return

        def inteiro(campo, padrao=0):
            try:
                return int(dados.get(campo, padrao) or padrao)
            except (ValueError, TypeError, OverflowError):
                return padrao

        saldo_atual = inteiro("moedas")
        ultima_mensagem = inteiro("ultima_mensagem")
        boost_ate = inteiro("boost_ate")
        inicio_hora = inteiro("recompensa_hora_inicio")
        total_hora = inteiro("recompensa_hora_total")
        mensagens_bonus = inteiro("mensagens_bonus")
        ultima_contada = inteiro("ultima_mensagem_contada")
        ultimo_texto = str(dados.get("ultima_mensagem_texto", "") or "").casefold()

        # Inicia uma nova janela de limite por hora.
        if inicio_hora <= 0 or agora < inicio_hora or agora - inicio_hora >= 3600:
            inicio_hora = agora
            total_hora = 0

        # A sequência de conversa é reiniciada após 2 minutos sem participação.
        if ultima_contada and agora - ultima_contada > 120:
            mensagens_bonus = 0

        mensagem_contada = False

        if mensagem_valida:
            intervalo_ok = (
                ultima_contada == 0
                or agora - ultima_contada >= 3
            )
            mensagem_repetida = (
                bool(ultimo_texto)
                and texto_normalizado == ultimo_texto
            )

            if intervalo_ok and not mensagem_repetida:
                mensagens_bonus += 1
                ultima_contada = agora
                ultimo_texto = texto_normalizado
                mensagem_contada = True

        # Calcula as duas fontes de recompensa.
        recompensa_tempo = 0
        if mensagem_valida and agora - ultima_mensagem >= INTERVALO_RECOMPENSA:
            recompensa_tempo = RECOMPENSA
            ultima_mensagem = agora

        recompensa_bonus = 0
        if mensagem_contada and mensagens_bonus >= MENSAGENS_PARA_BONUS:
            recompensa_bonus = BONUS_POR_MENSAGENS
            mensagens_bonus = 0

        recompensa_base = recompensa_tempo + recompensa_bonus
        multiplicador = 2 if boost_ate > agora else 1
        recompensa_solicitada = recompensa_base * multiplicador

        # O limite considera as moedas realmente creditadas, inclusive Boost 2x.
        restante_hora = max(0, LIMITE_RECOMPENSA_HORA - total_hora)
        moedas_creditadas = min(recompensa_solicitada, restante_hora)

        novas_moedas = saldo_atual + moedas_creditadas
        novo_total_hora = total_hora + moedas_creditadas

        atualizacao = {
            "nome": nome,
            "channelId": channel_id,
            "moedas": novas_moedas,
            "ultima_mensagem": ultima_mensagem,
            "boost_ate": boost_ate,
            "recompensa_hora_inicio": inicio_hora,
            "recompensa_hora_total": novo_total_hora,
            "mensagens_bonus": mensagens_bonus,
            "ultima_mensagem_contada": ultima_contada,
            "ultima_mensagem_texto": ultimo_texto
        }

        sucesso = firebase_atualizar_jogador(channel_id, atualizacao)

        if not sucesso:
            return

        with lock_jogadores:
            jogador["nome"] = nome
            jogador["moedas"] = novas_moedas
            jogador["ultima_mensagem"] = ultima_mensagem

    if moedas_creditadas:
        partes = []

        if recompensa_tempo:
            partes.append(f"{recompensa_tempo} por participação")

        if recompensa_bonus:
            partes.append(f"{recompensa_bonus} de bônus por mensagens")

        if multiplicador == 2:
            partes.append("Boost 2x")

        print(
            f"[MOEDAS] {nome} recebeu {moedas_creditadas} moeda(s) "
            f"({' + '.join(partes)}). "
            f"Limite da hora: {novo_total_hora}/{LIMITE_RECOMPENSA_HORA}. "
            f"Saldo: {novas_moedas}"
        )
    elif mensagem_contada and recompensa_base:
        print(
            f"[MOEDAS] Limite de {LIMITE_RECOMPENSA_HORA} moedas/hora "
            f"atingido para {nome}."
        )



def descontar_moedas(channel_id, nome, preco):

    # O mesmo lock usado pela recompensa impede que dois
    # processos leiam e sobrescrevam o saldo simultaneamente.
    with lock_transacoes:

        dados = firebase_buscar_jogador(channel_id)

        if dados is None:

            print(
                "[MOEDAS] Não foi possível consultar "
                "o saldo do jogador."
            )

            return None, 0

        try:
            saldo = int(dados.get("moedas", 0))

        except (ValueError, TypeError):
            saldo = 0

        if saldo < preco:
            return False, saldo

        novo_saldo = saldo - preco

        sucesso = firebase_atualizar_jogador(
            channel_id,
            {
                "nome": nome,
                "channelId": channel_id,
                "moedas": novo_saldo,
                "sons_tocados": ler_int(dados, "sons_tocados") + 1
            }
        )

        if not sucesso:
            return None, saldo

        with lock_jogadores:
            if channel_id in jogadores:
                jogadores[channel_id]["moedas"] = novo_saldo
                jogadores[channel_id]["nome"] = nome

        return True, novo_saldo


# ============================================================
# CATÁLOGO DE SONS - FIREBASE + GITHUB
# ============================================================

def carregar_precos_firebase():

    resposta = sessao.get(
        FIREBASE_SONS_URL,
        timeout=TIMEOUT_FIREBASE
    )

    resposta.raise_for_status()

    precos = resposta.json()

    if not isinstance(precos, dict):
        raise ValueError(
            "O nó /sons do Firebase precisa conter um objeto JSON."
        )

    precos_normalizados = {}

    for nome, valor in precos.items():

        if not isinstance(nome, str) or not nome.strip():
            continue

        # bool é uma subclasse de int em Python, por isso é rejeitado.
        if isinstance(valor, bool):
            continue

        try:
            preco = int(valor)
        except (ValueError, TypeError, OverflowError):
            print(f"[SONS] Preço inválido para {nome}.")
            continue

        if preco < 0 or str(valor).strip() != str(preco):
            print(f"[SONS] Preço inválido para {nome}.")
            continue

        precos_normalizados[nome.casefold()] = preco

    return precos_normalizados


def carregar_catalogo(forcar=False):

    agora = time.time()

    with lock_catalogo:

        if (
            not forcar
            and catalogo_cache["sons"]
            and agora - catalogo_cache["atualizado"]
            < INTERVALO_CACHE_SONS
        ):

            return catalogo_cache["sons"]
        print("[SONS] Atualizando catálogo...")

        try:

            # Os preços agora vêm do Firebase.
            precos = carregar_precos_firebase()

            # O GitHub continua sendo usado somente para localizar
            # e obter os arquivos de áudio.
            resposta = sessao.get(
                GITHUB_API,
                headers={
                    "User-Agent": "YouTubeChatBot"
                },
                timeout=TIMEOUT_FIREBASE
            )

            resposta.raise_for_status()

            arquivos = resposta.json()

            if not isinstance(arquivos, list):
                raise ValueError("Resposta inválida do GitHub.")

            novo_catalogo = {}

            for item in arquivos:

                if item.get("type") != "file":
                    continue

                nome_arquivo = item.get("name", "")
                nome_lower = nome_arquivo.lower()

                if not nome_lower.endswith(EXTENSOES_AUDIO):
                    continue

                nome_som = nome_arquivo.rsplit(".", 1)[0]
                chave = nome_som.casefold()

                preco = precos.get(chave)

                if preco is None:

                    print(
                        f"[SONS] Ignorando {nome_arquivo}: "
                        "preço não configurado no Firebase."
                    )
                    continue

                url = item.get("download_url")

                if not url:
                    continue

                novo_catalogo[chave] = {
                    "nome": nome_som,
                    "arquivo": nome_arquivo,
                    "preco": preco,
                    "url": url
                }

            catalogo_cache["sons"] = novo_catalogo
            catalogo_cache["atualizado"] = agora

            print(
                f"[SONS] {len(novo_catalogo)} áudio(s) carregado(s)."
            )

            return novo_catalogo

        except Exception as erro:

            print("[SONS] Erro ao carregar catálogo:", erro)

            if catalogo_cache["sons"]:

                print("[SONS] Utilizando catálogo em cache.")

                return catalogo_cache["sons"]

            return {}


# ============================================================
# USER AGENT
# ============================================================

def configurar_user_agent():

    metadata = {

        "brands": [
            {"brand": "Not:A-Brand", "version": "8"},
            {"brand": "Chromium", "version": "156"}
        ],

        "fullVersionList": [
            {"brand": "Not:A-Brand", "version": "8.0.0.0"},
            {"brand": "Chromium", "version": VERSAO}
        ],

        "platform": "Linux",
        "platformVersion": "6.8.0",
        "architecture": "x86",
        "model": "",
        "mobile": False,
        "bitness": "64",
        "wow64": False
    }

    driver.execute_cdp_cmd(
        "Network.setUserAgentOverride",
        {
            "userAgent": USER_AGENT,
            "acceptLanguage": "pt-BR,pt;q=0.9,en-US;q=0.8",
            "platform": "Linux",
            "userAgentMetadata": metadata
        }
    )

    print("[NAVEGADOR] User-Agent configurado.")


# ============================================================
# CHROMIUM
# ============================================================

def abrir_navegador():

    global driver

    print("[NAVEGADOR] Iniciando Chromium...")

    os.makedirs(PERFIL, exist_ok=True)

    opcoes = Options()

    opcoes.binary_location = CHROMIUM

    opcoes.add_argument("--headless=new")
    opcoes.add_argument("--no-sandbox")
    opcoes.add_argument("--disable-dev-shm-usage")
    opcoes.add_argument("--disable-gpu")
    opcoes.add_argument("--disable-notifications")
    opcoes.add_argument("--disable-features=UseDBus")
    opcoes.add_argument("--window-size=1280,900")
    opcoes.add_argument("--lang=pt-BR")

    opcoes.add_argument(
        "--user-data-dir=" + PERFIL
    )

    caminho_driver = (
        CHROMEDRIVER
        if os.path.exists(CHROMEDRIVER)
        else shutil.which("chromedriver")
    )

    if not caminho_driver:

        raise FileNotFoundError(
            "ChromeDriver não encontrado."
        )

    driver = webdriver.Chrome(
        service=Service(caminho_driver),
        options=opcoes
    )

    driver.set_page_load_timeout(60)

    configurar_user_agent()

    print("[NAVEGADOR] Chromium iniciado.")

    return driver


# ============================================================
# ABRIR CHAT
# ============================================================

def abrir_chat(live_id=None):

    live_id = live_id or LIVE_ID

    url = (
        "https://www.youtube.com/live_chat"
        "?is_popout=1&v=" + live_id
    )

    print("[CHAT] Abrindo:", url)

    driver.get(url)

    espera = WebDriverWait(driver, 40)

    espera.until(
        EC.presence_of_element_located(
            (By.CSS_SELECTOR, SELETOR_CAMPO)
        )
    )

    espera.until(
        EC.presence_of_element_located(
            (By.CSS_SELECTOR, "#send-button")
        )
    )

    print("[CHAT] Página carregada.")
    print("[CHAT] Título:", driver.title)
    print("[CHAT] URL atual:", driver.current_url)
    print("[CHAT] Campo de mensagem pronto!")


# ============================================================
# LOCALIZAR CAMPO
# ============================================================

def encontrar_campo_mensagem():

    try:

        return driver.find_element(
            By.CSS_SELECTOR,
            SELETOR_CAMPO
        )

    except Exception:
        pass

    try:

        return driver.find_element(
            By.CSS_SELECTOR,
            "#input[contenteditable]"
        )

    except Exception:
        return None


# ============================================================
# DIAGNÓSTICO DE ERROS
# ============================================================

def diagnosticar_envio():

    global driver

    print("\n========== DIAGNÓSTICO DO ENVIO ==========")

    try:

        dialogos = driver.find_elements(
            By.CSS_SELECTOR,
            "yt-live-chat-upsell-dialog-renderer"
        )

        print(
            "[DEBUG] Quantidade de avisos:",
            len(dialogos)
        )

        for i, dialogo in enumerate(dialogos):

            print(f"\n[DEBUG] AVISO #{i + 1}")

            print(
                "[DEBUG] Texto:",
                repr(dialogo.text)
            )

            print(
                "[DEBUG] HTML:",
                dialogo.get_attribute("innerHTML")[:5000]
            )

        resultado = driver.execute_script("""
            const campo = document.querySelector(arguments[0]);

            if (!campo) return "Campo não encontrado";

            const rect = campo.getBoundingClientRect();

            const elemento = document.elementFromPoint(
                rect.left + rect.width / 2,
                rect.top + rect.height / 2
            );

            return elemento ? {
                tag: elemento.tagName,
                id: elemento.id,
                classe: elemento.className,
                texto: elemento.innerText
            } : null;
        """, SELETOR_CAMPO)

        print("[DEBUG] Elemento:", resultado)

        caminho = "/sdcard/Download/chat_erro.png"

        driver.save_screenshot(caminho)

        print("[DEBUG] Screenshot salvo em:", caminho)

    except Exception as erro:

        print("[DEBUG] Erro no diagnóstico:", erro)

    print("==========================================\n")


# ============================================================
# ENVIAR MENSAGEM - OTIMIZADO
# ============================================================

def enviar_mensagem(mensagem):

    if not mensagem or not mensagem.strip():
        return False

    if not driver:
        print("[ENVIO] Navegador não iniciado.")
        return False

    with lock_envio:

        try:

            campo = encontrar_campo_mensagem()

            if not campo:
                print("[ENVIO] Campo não encontrado.")
                return False

            driver.execute_script(
                "arguments[0].focus();",
                campo
            )

            texto_atual = (
                campo.get_attribute("textContent") or ""
            )

            if texto_atual:

                campo.send_keys(
                    Keys.CONTROL,
                    "a"
                )

                campo.send_keys(Keys.BACKSPACE)

            # Insere o texto pelo protocolo CDP do Chromium.
            # Se falhar, usa a digitação tradicional como alternativa.
            try:
                driver.execute_cdp_cmd(
                    "Input.insertText",
                    {"text": mensagem}
                )
            except Exception as erro_cdp:
                print(
                    "[ENVIO] Inserção rápida falhou; "
                    "usando digitação tradicional:",
                    erro_cdp
                )
                campo.send_keys(mensagem)

            botao = driver.find_element(
                By.CSS_SELECTOR,
                "#send-button"
            )

            if not botao.is_displayed():
                print("[ENVIO] Botão não está visível.")
                diagnosticar_envio()
                return False

            botao.click()

            try:

                WebDriverWait(
                    driver,
                    TIMEOUT_ENVIO,
                    poll_frequency=0.1
                ).until(
                    lambda navegador: not (
                        navegador.find_element(
                            By.CSS_SELECTOR,
                            SELETOR_CAMPO
                        ).get_attribute("textContent") or ""
                    ).strip()
                )

                print("[ENVIO] Mensagem enviada!")
                return True

            except TimeoutException:

                print(
                    "[ENVIO] O envio não foi confirmado."
                )

                diagnosticar_envio()
                return False

        except Exception as erro:

            print("[ENVIO] Erro:", erro)
            diagnosticar_envio()
            return False

# ============================================================
# CONTROLE DE COOLDOWN
# ============================================================

def comando_disponivel(channel_id, tabela, intervalo):

    agora = time.time()

    with lock_cooldown:

        ultimo = tabela.get(channel_id, 0)

        if agora - ultimo < intervalo:
            return False

        tabela[channel_id] = agora

        return True


# ============================================================
# COMANDO !HELP
# ============================================================

def comando_help(channel_id, nome):

    if not comando_disponivel(
        channel_id,
        ultimo_help,
        INTERVALO_COMANDO
    ):
        return

    enviar_mensagem(
        f"{nome}, comandos: !moedas | !diario | !top | !perfil | !pescar | !sorteio | !loja | !sons | !som nome"
    )


# ============================================================
# COMANDO !MOEDAS
# ============================================================

def comando_moedas(channel_id, nome):

    if not comando_disponivel(
        channel_id,
        ultimo_moedas,
        INTERVALO_COMANDO
    ):
        return

    dados = firebase_buscar_jogador(channel_id)

    if dados is None:
        enviar_mensagem(
            f"{nome}, não consegui consultar seu saldo."
        )

        return

    try:
        moedas = int(dados.get("moedas", 0))

    except (ValueError, TypeError):
        moedas = 0

    with lock_jogadores:

        if channel_id in jogadores:
            jogadores[channel_id]["moedas"] = moedas

    enviar_mensagem(
        f"{nome}, você tem {moedas} moedas!"
    )


# ============================================================
# COMANDO !SONS
# ============================================================

def comando_sons(channel_id, nome):

    catalogo = carregar_catalogo()

    if not catalogo:

        enviar_mensagem(
            "Não há sons disponíveis no momento."
        )

        return

    lista = sorted(
        catalogo.values(),
        key=lambda som: som["nome"].casefold()
    )

    partes = []
    mensagem_atual = "Sons disponíveis: "

    for som in lista:

        texto = (
            f'{som["nome"]} ({som["preco"]} moedas)'
        )

        if len(mensagem_atual) + len(texto) + 3 > 180:

            partes.append(mensagem_atual)
            mensagem_atual = texto

        else:

            if mensagem_atual != "Sons disponíveis: ":
                mensagem_atual += " | "

            mensagem_atual += texto

    if mensagem_atual.strip():
        partes.append(mensagem_atual)

    for parte in partes:
        enviar_mensagem(parte)


# ============================================================
# COMANDO !SOM
# ============================================================

def comando_som(channel_id, nome, argumento):

    if not comando_disponivel(
        channel_id,
        ultimo_som,        INTERVALO_SOM
    ):        return
    if not argumento:

        enviar_mensagem(
            f"{nome}, use !som nome. Consulte !sons para ver os áudios."
        )

        return

    nome_pedido = argumento.strip().casefold()

    catalogo = carregar_catalogo()

    som = catalogo.get(nome_pedido)

    if not som:

        enviar_mensagem(
            f"{nome}, esse som não existe. Use !sons para consultar."
        )

        return

    preco = som["preco"]

    sucesso, saldo = descontar_moedas(
        channel_id,
        nome,
        preco
    )

    if sucesso is None:

        enviar_mensagem(
            f"{nome}, não consegui processar seu pagamento. Tente novamente."
        )

        return

    if sucesso is False:

        enviar_mensagem(
            f"{nome}, você precisa de {preco} moedas. "
            f"Seu saldo: {saldo}."
        )

        return

    pedido = {
        "id": next(contador_sons),
        "nome": som["nome"],
        "arquivo": som["arquivo"],
        "url": som["url"],
        "preco": preco,
        "jogador": nome
    }

    fila_sons.put(pedido)

    print(
        f'[SONS] {nome} pediu {som["nome"]} '
        f'por {preco} moedas. Saldo restante: {saldo}'
    )

    enviar_mensagem(
        f"{nome}, som {som['nome']} comprado por "
        f"{preco} moedas! Saldo: {saldo}."
    )


# ============================================================
# ECONOMIA, PERFIL, LOJA, PESCA E SORTEIO
# ============================================================

def ler_int(dados, chave, padrao=0):
    try:
        return int(dados.get(chave, padrao) or padrao)
    except (ValueError, TypeError):
        return padrao


def comando_diario(channel_id, nome):
    with lock_transacoes:
        dados = firebase_buscar_jogador(channel_id)
        if dados is None:
            enviar_mensagem(f"{nome}, não consegui consultar sua conta.")
            return

        agora = int(time.time())
        ultimo = ler_int(dados, "ultimo_diario")
        restante = INTERVALO_DIARIO - (agora - ultimo)

        if restante > 0:
            horas = restante // 3600
            minutos = (restante % 3600) // 60
            enviar_mensagem(
                f"{nome}, você já resgatou seu diário! "
                f"Volte em {horas}h {minutos}min."
            )
            return

        boost_ate = ler_int(dados, "boost_ate")
        multiplicador = 2 if boost_ate > agora else 1
        ganho = RECOMPENSA_DIARIA * multiplicador
        saldo = ler_int(dados, "moedas") + ganho

        sucesso = firebase_atualizar_jogador(channel_id, {
            "nome": nome,
            "channelId": channel_id,
            "moedas": saldo,
            "ultimo_diario": agora
        })
        if not sucesso:
            enviar_mensagem(f"{nome}, não consegui entregar sua recompensa.")
            return

        with lock_jogadores:
            if channel_id in jogadores:
                jogadores[channel_id]["moedas"] = saldo

    enviar_mensagem(
        f"🎁 {nome}, você recebeu {ganho} moedas do diário! "
        f"Saldo: {saldo}."
    )


def comando_top(channel_id, nome):
    if not comando_disponivel(channel_id, ultimo_top, 15):
        return

    try:
        resposta = sessao.get(
            FIREBASE_URL + "jogadores.json",
            timeout=TIMEOUT_FIREBASE
        )
        resposta.raise_for_status()
        dados = resposta.json()
        if not isinstance(dados, dict):
            dados = {}
    except Exception as erro:
        print("[TOP] Erro ao consultar ranking:", erro)
        enviar_mensagem("Não consegui carregar o ranking agora.")
        return

    ranking = []
    for jogador_id, info in dados.items():
        if not isinstance(info, dict):
            continue
        ranking.append((
            ler_int(info, "moedas"),
            str(info.get("nome", "Jogador"))
        ))

    ranking.sort(key=lambda item: item[0], reverse=True)
    if not ranking:
        enviar_mensagem("Ainda não há jogadores no ranking.")
        return

    linhas = [
        f"{i + 1}º {apelido}: {saldo}"
        for i, (saldo, apelido) in enumerate(ranking[:5])
    ]
    enviar_mensagem("🏆 TOP moedas: " + " | ".join(linhas))


def comando_perfil(channel_id, nome):
    if not comando_disponivel(channel_id, ultimo_perfil, 10):
        return

    dados = firebase_buscar_jogador(channel_id)
    if dados is None:
        enviar_mensagem(f"{nome}, não consegui carregar seu perfil.")
        return

    saldo = ler_int(dados, "moedas")
    pescarias = ler_int(dados, "pescarias")
    tesouros = ler_int(dados, "tesouros")
    moedas_pescando = ler_int(dados, "moedas_pescando")
    sons_tocados = ler_int(dados, "sons_tocados")
    agora = int(time.time())
    boost_ate = ler_int(dados, "boost_ate")
    sorte_ate = ler_int(dados, "sorte_pesca_ate")
    boost_texto = (
        f" | Boost 2x ativo por {(boost_ate - agora) // 60} min"
        if boost_ate > agora else ""
    )
    sorte_texto = (
        f" | 🍀 Sorte na pesca por {(sorte_ate - agora) // 60} min"
        if sorte_ate > agora else ""
    )

    enviar_mensagem(
        f"👤 {nome} | 🪙 {saldo} moedas | 🎣 {pescarias} pescarias "
        f"({tesouros} tesouros, +{moedas_pescando} moedas) "
        f"| 🔊 {sons_tocados} sons tocados{boost_texto}{sorte_texto}"
    )


def comando_loja(channel_id, nome, argumento=""):
    if not comando_disponivel(channel_id, ultimo_loja, 5):
        return

    partes = argumento.strip().split()
    if not partes:
        enviar_mensagem(
            f"🛒 Loja: boost2x {PRECO_BOOST_2X} moedas (2x recompensas/10min); "
            f"sortepesca {PRECO_SORTE_PESCA} moedas (mais raros e baús/10min). "
            f"Comprar: !loja comprar boost2x ou !loja comprar sortepesca."
        )
        return

    if len(partes) != 2 or partes[0].casefold() != "comprar":
        enviar_mensagem(
            f"{nome}, use !loja ou !loja comprar boost2x/sortepesca."
        )
        return

    item = partes[1].casefold()
    if item not in ("boost2x", "dobro", "sortepesca", "sorte"):
        enviar_mensagem(f"{nome}, esse item não existe. Consulte !loja.")
        return

    with lock_transacoes:
        dados = firebase_buscar_jogador(channel_id)
        if dados is None:
            enviar_mensagem(f"{nome}, não consegui consultar seu saldo.")
            return

        saldo = ler_int(dados, "moedas")
        agora = int(time.time())

        if item in ("boost2x", "dobro"):
            preco = PRECO_BOOST_2X
            campo_tempo = "boost_ate"
            duracao = DURACAO_BOOST_2X
            inicio = max(agora, ler_int(dados, campo_tempo))
            novo_tempo = inicio + duracao
            descricao = "Boost 2x"
        else:
            preco = PRECO_SORTE_PESCA
            campo_tempo = "sorte_pesca_ate"
            duracao = DURACAO_SORTE_PESCA
            inicio = max(agora, ler_int(dados, campo_tempo))
            novo_tempo = inicio + duracao
            descricao = "Sorte na pesca"

        if saldo < preco:
            enviar_mensagem(
                f"{nome}, {descricao} custa {preco} moedas. Você tem {saldo}."
            )
            return

        novo_saldo = saldo - preco

        if not firebase_atualizar_jogador(channel_id, {
            "nome": nome,
            "channelId": channel_id,
            "moedas": novo_saldo,
            campo_tempo: novo_tempo
        }):
            enviar_mensagem(f"{nome}, não consegui concluir a compra.")
            return

        with lock_jogadores:
            if channel_id in jogadores:
                jogadores[channel_id]["moedas"] = novo_saldo

    enviar_mensagem(
        f"🛒 {nome} comprou {descricao} por {preco} moedas! "
        f"Duração: 10 minutos. Saldo: {novo_saldo}."
    )


def comando_pescar(channel_id, nome):
    if not comando_disponivel(channel_id, ultimo_pesca, INTERVALO_PESCA):
        enviar_mensagem(f"{nome}, espere 60 segundos para pescar novamente.")
        return

    with lock_transacoes:
        dados = firebase_buscar_jogador(channel_id)
        if dados is None:
            enviar_mensagem(f"{nome}, não consegui iniciar sua pescaria.")
            return

        saldo = ler_int(dados, "moedas")
        agora = int(time.time())
        boost_ate = ler_int(dados, "boost_ate")
        sorte_ate = ler_int(dados, "sorte_pesca_ate")
        multiplicador = 2 if boost_ate > agora else 1

        if sorte_ate > agora:
            resultados = [
                ("comum", 34.7),
                ("raro", 35.0),
                ("bau", 15.0),
                ("lixo", 10.0),
                ("tubarao", 5.0),
                ("lendario", 0.3)
            ]
        else:
            resultados = [
                ("comum", 44.9),
                ("raro", 25.0),
                ("bau", 10.0),
                ("lixo", 15.0),
                ("tubarao", 5.0),
                ("lendario", 0.1)
            ]

        resultado = random.choices(
            [item[0] for item in resultados],
            weights=[item[1] for item in resultados],
            k=1
        )[0]

        ganho = 0
        perda = 0

        if resultado == "comum":
            ganho = random.randint(2, 5)
            mensagem = f"🐟 {nome} pescou um peixe comum e ganhou {ganho} moedas!"
        elif resultado == "raro":
            ganho = random.randint(6, 12)
            mensagem = f"🐠 {nome} pescou um peixe raro e ganhou {ganho} moedas!"
        elif resultado == "bau":
            ganho = random.randint(15, 30)
            mensagem = f"💎 {nome} encontrou um baú e ganhou {ganho} moedas!"
        elif resultado == "lixo":
            mensagem = f"👢 {nome} pescou uma bota velha. Que azar!"
        elif resultado == "tubarao":
            perda = min(saldo, random.randint(2, 5))
            mensagem = f"🦈 Um tubarão atacou {nome}! Você perdeu {perda} moedas!"
        else:
            ganho = 100
            mensagem = f"🌟 LENDÁRIO! {nome} pescou um peixe lendário e ganhou 100 moedas!"

        ganho *= multiplicador
        novo_saldo = saldo + ganho - perda
        pescarias = ler_int(dados, "pescarias") + 1
        tesouros = ler_int(dados, "tesouros") + (1 if resultado == "bau" else 0)
        moedas_pescando = ler_int(dados, "moedas_pescando") + ganho

        atualizacao = {
            "nome": nome,
            "channelId": channel_id,
            "moedas": novo_saldo,
            "pescarias": pescarias,
            "tesouros": tesouros,
            "moedas_pescando": moedas_pescando
        }

        if not firebase_atualizar_jogador(channel_id, atualizacao):
            enviar_mensagem(f"{nome}, houve um erro ao registrar sua pescaria.")
            return

        with lock_jogadores:
            if channel_id in jogadores:
                jogadores[channel_id]["moedas"] = novo_saldo

    enviar_mensagem(mensagem + f" Saldo: {novo_saldo}.")


def comando_sorteio(channel_id, nome):
    with lock_sorteio:
        if not sorteio_ativo:
            enviar_mensagem(f"{nome}, não há sorteio ativo no momento.")
            return
        if channel_id in sorteio_participantes:
            enviar_mensagem(f"{nome}, você já está participando do sorteio!")
            return
        sorteio_participantes[channel_id] = nome
        quantidade = len(sorteio_participantes)

    enviar_mensagem(
        f"🎟️ {nome} entrou no sorteio! Participantes: {quantidade}."
    )


def controlar_sorteio_terminal(comando):
    global sorteio_ativo, sorteio_premio, sorteio_participantes

    partes = comando.strip().split()
    if len(partes) < 2:
        print("[SORTEIO] Use :sorteio iniciar <premio> ou :sorteio encerrar")
        return True

    acao = partes[1].casefold()
    if acao == "iniciar":
        if len(partes) < 3:
            print("[SORTEIO] Informe o prêmio. Exemplo: :sorteio iniciar 100")
            return True
        try:
            premio = int(partes[2])
            if premio < 0:
                raise ValueError
        except ValueError:
            print("[SORTEIO] O prêmio deve ser um número inteiro não negativo.")
            return True

        with lock_sorteio:
            if sorteio_ativo:
                print("[SORTEIO] Já existe um sorteio ativo.")
                return True
            sorteio_ativo = True
            sorteio_premio = premio
            sorteio_participantes = {}

        enviar_mensagem(
            f"🎉 SORTEIO ABERTO! Digite !sorteio para participar. "
            f"Prêmio: {premio} moedas!"
        )
        print("[SORTEIO] Sorteio iniciado.")
        return True

    if acao == "encerrar":
        with lock_sorteio:
            if not sorteio_ativo:
                print("[SORTEIO] Não há sorteio ativo.")
                return True
            participantes = list(sorteio_participantes.items())
            premio = sorteio_premio
            sorteio_ativo = False
            sorteio_participantes = {}

        if not participantes:
            enviar_mensagem("🎟️ Sorteio encerrado sem participantes.")
            return True

        vencedor_id, vencedor_nome = random.choice(participantes)
        if premio > 0:
            with lock_transacoes:
                dados = firebase_buscar_jogador(vencedor_id)
                if dados is not None:
                    saldo = ler_int(dados, "moedas") + premio
                    if firebase_atualizar_jogador(vencedor_id, {
                        "nome": vencedor_nome,
                        "channelId": vencedor_id,
                        "moedas": saldo
                    }):
                        with lock_jogadores:
                            if vencedor_id in jogadores:
                                jogadores[vencedor_id]["moedas"] = saldo
                    else:
                        print("[SORTEIO] Não foi possível entregar o prêmio.")
                else:
                    print("[SORTEIO] Não foi possível consultar o vencedor.")
        enviar_mensagem(
            f"🏆 O vencedor do sorteio é {vencedor_nome}! "
            f"Prêmio: {premio} moedas!"
        )
        print("[SORTEIO] Sorteio encerrado.")
        return True

    print("[SORTEIO] Use :sorteio iniciar <premio> ou :sorteio encerrar")
    return True


# ============================================================
# FLASK - PÁGINA DO OBS
# ============================================================

app = Flask(__name__)


PAGINA_OBS = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">

<title>Sons do Bot</title>

<style>
* {
    box-sizing: border-box;
}

html, body {
    margin: 0;
    padding: 0;
    width: 100%;
    height: 100%;
    overflow: hidden;
    background: transparent;
}

#nome {
    display: none;
    position: absolute;
    bottom: 50px;
    left: 50%;
    transform: translateX(-50%);
    padding: 4px 8px;
    background: transparent;
    color: white;
    font-family: Arial, sans-serif;
    font-size: 26px;
    font-weight: bold;
    white-space: nowrap;
    -webkit-text-stroke: 1.5px #000;
    paint-order: stroke fill;
    text-shadow:
        -2px -2px 0 #000,
         0   -2px 0 #000,
         2px -2px 0 #000,
        -2px  0   0 #000,
         2px  0   0 #000,
        -2px  2px 0 #000,
         0    2px 0 #000,
         2px  2px 0 #000;
}
</style>
</head>

<body>

<div id="nome"></div>
<audio id="audio"></audio>

<script>
const audio = document.getElementById("audio");
const nomeElemento = document.getElementById("nome");

let reproduzindo = false;
let buscando = false;
async function buscarProximo() {
    if (buscando || reproduzindo) return;

    buscando = true;

    try {

        const resposta = await fetch("/api/proximo", {            cache: "no-store"
        });

        if (!resposta.ok) {
            throw new Error("Erro na API");
        }

        const dados = await resposta.json();

        if (!dados.som) {
            buscando = false;
            setTimeout(buscarProximo, 100);
            return;
        }

        reproduzindo = true;
        buscando = false;

        nomeElemento.textContent = dados.som.jogador + " tocou " + dados.som.nome;
        nomeElemento.style.display = "block";

        audio.src = dados.som.url;

        await audio.play();

    } catch (erro) {

        console.error("Erro ao buscar/reproduzir:", erro);

        finalizar();
    }
}

function finalizar() {

    if (!reproduzindo && !buscando) return;

    reproduzindo = false;
    buscando = false;

    nomeElemento.style.display = "none";
    nomeElemento.textContent = "";

    audio.removeAttribute("src");
    audio.load();

    setTimeout(buscarProximo, 100);
}

audio.addEventListener("ended", finalizar);
audio.addEventListener("error", finalizar);

buscarProximo();
</script>

</body>
</html>
"""


@app.route("/")
def pagina_inicial():

    return Response(
        "Servidor do YouTube Bot está funcionando. "
        "Abra /obs no navegador do OBS.",
        mimetype="text/plain"
    )


@app.route("/obs")
def pagina_obs():

    return Response(
        PAGINA_OBS,
        mimetype="text/html"
    )


@app.route("/api/proximo")
def api_proximo():

    try:

        pedido = fila_sons.get_nowait()

        return jsonify({
            "som": pedido
        })

    except queue.Empty:

        return jsonify({
            "som": None
        })


@app.route("/api/status")
def api_status():

    return jsonify({
        "servidor": "online",
        "sons_na_fila": fila_sons.qsize()
    })


# ============================================================
# INICIAR FLASK
# ============================================================

def iniciar_flask():

    def executar():

        print(
            f"[FLASK] Servidor iniciado na porta {FLASK_PORTA}."
        )
        app.run(
            host=FLASK_HOST,
            port=FLASK_PORTA,
            debug=False,
            use_reloader=False,
            threaded=True
        )

    thread = threading.Thread(
        target=executar,
        daemon=True
    )

    thread.start()

    time.sleep(0.5)


# ============================================================
# PROCESSAR RECOMPENSAS EM SEGUNDO PLANO
# ============================================================

def processar_recompensas():

    print("[MOEDAS] Processador de recompensas iniciado.")

    while rodando or not fila_recompensas.empty():

        try:
            item = fila_recompensas.get(timeout=0.2)

        except queue.Empty:
            continue

        if item is None:
            fila_recompensas.task_done()
            break

        try:
            processar_jogador(
                item["channel_id"],
                item["nome"],
                item.get("mensagem", "")
            )

        except Exception as erro:
            print("[MOEDAS] Erro ao processar recompensa:", erro)

        finally:
            fila_recompensas.task_done()


# ============================================================
# PROCESSAR EVENTOS DO CHAT
# ============================================================

def processar_eventos_chat():

    print("[CHAT] Processador de mensagens iniciado.")

    while rodando:

        try:

            item = fila_eventos_chat.get(timeout=0.2)

        except queue.Empty:
            continue

        if item is None:
            break

        try:

            channel_id = item["channel_id"]
            nome = item["nome"]
            mensagem = item["mensagem"]

            # Recompensas são tratadas em paralelo para que
            # operações no Firebase não atrasem os comandos.
            fila_recompensas.put({
                "channel_id": channel_id,
                "nome": nome,
                "mensagem": mensagem
            })

            texto = mensagem.strip()
            comando = texto.lower()

            if comando == "!help":

                comando_help(
                    channel_id,
                    nome
                )

            elif comando == "!moedas":

                comando_moedas(
                    channel_id,
                    nome
                )

            elif comando == "!sons":

                comando_sons(
                    channel_id,
                    nome
                )

            elif comando == "!diario":
                comando_diario(channel_id, nome)

            elif comando == "!top":
                comando_top(channel_id, nome)

            elif comando == "!perfil":
                comando_perfil(channel_id, nome)

            elif comando == "!pescar":
                comando_pescar(channel_id, nome)

            elif comando == "!sorteio":
                comando_sorteio(channel_id, nome)

            elif comando == "!loja" or comando.startswith("!loja "):
                partes = texto.split(maxsplit=1)
                argumento = partes[1] if len(partes) > 1 else ""
                comando_loja(channel_id, nome, argumento)

            elif comando == "!som" or comando.startswith("!som "):

                partes = texto.split(maxsplit=1)

                argumento = (
                    partes[1]
                    if len(partes) > 1
                    else ""
                )

                comando_som(
                    channel_id,
                    nome,
                    argumento
                )

        except Exception as erro:

            print(
                "[CHAT] Erro ao processar mensagem:",
                erro
            )

        finally:
            fila_eventos_chat.task_done()


# ============================================================
# RECEBER CHAT - MAIS RÁPIDO
# ============================================================

def ler_chat(chat):

    print("[CHAT] Monitorando mensagens...")

    while rodando and chat.is_alive():

        try:

            dados = chat.get()

            if chat.is_replay():
                continue

            for item in dados.sync_items():

                nome = item.author.name

                channel_id = getattr(
                    item.author,
                    "channelId",
                    None
                )

                mensagem = item.message

                print(
                    f"[CHAT] {nome} ({channel_id}): {mensagem}"
                )

                if not channel_id:

                    print(
                        "[CHAT] Mensagem sem channelId. "
                        "Ignorando recompensa."
                    )

                    continue

                fila_eventos_chat.put({
                    "channel_id": channel_id,
                    "nome": nome,
                    "mensagem": mensagem
                })

            time.sleep(0.05)

        except Exception as erro:

            print("[CHAT] Erro ao ler mensagens:", erro)

            time.sleep(0.5)


# ============================================================
# ENTRADA PELO TERMINAL
# ============================================================

def receber_mensagens():

    while rodando:

        try:

            mensagem = input()

            if mensagem.strip().lower().startswith(":sorteio"):
                controlar_sorteio_terminal(mensagem)
                continue

            if mensagem.strip().lower() == ":sair":

                fila_mensagens.put(None)

                break

            if mensagem.strip():
                fila_mensagens.put(mensagem)

        except EOFError:

            fila_mensagens.put(None)
            break

        except Exception as erro:

            print("[TERMINAL] Erro:", erro)
            break


# ============================================================
# MONITORAR LIVE
# ============================================================

def extrair_live_id(entrada):

    """Aceita ID puro ou link do YouTube e retorna o ID do vídeo."""

    import re
    from urllib.parse import urlparse, parse_qs

    texto = (entrada or "").strip()

    if not texto:
        return None

    if re.fullmatch(r"[a-zA-Z0-9_-]{11}", texto):
        return texto

    if "://" not in texto:
        texto = "https://" + texto

    try:
        url = urlparse(texto)
        host = (url.netloc or "").lower()
        caminho = url.path.strip("/")

        if host.endswith("youtu.be"):
            candidato = caminho.split("/")[0]
            if re.fullmatch(r"[a-zA-Z0-9_-]{11}", candidato):
                return candidato

        parametros = parse_qs(url.query)
        candidato = (parametros.get("v") or [None])[0]
        if candidato and re.fullmatch(r"[a-zA-Z0-9_-]{11}", candidato):
            return candidato

        partes = caminho.split("/")
        for marcador in ("live", "shorts", "embed", "v"):
            if marcador in partes:
                indice = partes.index(marcador)
                if indice + 1 < len(partes):
                    candidato = partes[indice + 1]
                    if re.fullmatch(r"[a-zA-Z0-9_-]{11}", candidato):
                        return candidato
    except Exception:
        pass

    return None


def live_esta_ativa(video_id):

    """Valida o candidato usando pytchat, sem depender da API oficial."""

    chat = None

    try:
        chat = pytchat.create(
            video_id=video_id,
            interruptable=False
        )

        if not chat.is_alive():
            return False

        dados = chat.get()

        if chat.is_replay():
            return False

        return True

    except Exception as erro:
        print(f"[LIVE] Não foi possível validar {video_id}: {erro}")
        return False

    finally:
        if chat:
            try:
                chat.terminate()
            except Exception:
                pass


def pegar_live_id(handle):

    """
    Procura a live atual/mais recente do canal.
    Primeiro verifica /live (incluindo redirecionamentos); depois
    consulta /streams e /videos para localizar IDs no HTML.
    """

    import re
    from urllib.parse import urlparse, parse_qs

    canal = handle.strip().lstrip("@")
    base = f"https://www.youtube.com/@{canal}"

    cabecalhos = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8"
    }

    encontrados = []

    for sufixo in ("/live", "/streams", "/videos"):

        try:
            resposta = sessao.get(
                base + sufixo,
                headers=cabecalhos,
                timeout=TIMEOUT_BUSCA_LIVE,
                allow_redirects=True
            )
            resposta.raise_for_status()

            # Se /live redirecionou diretamente para uma transmissão,
            # tenta extrair o ID da URL final.
            id_url = extrair_live_id(resposta.url)
            if id_url and id_url not in encontrados:
                encontrados.append(id_url)

            html = resposta.text
            padroes = (
                r'"videoId"\s*:\s*"([a-zA-Z0-9_-]{11})"',                r'"externalVideoId"\s*:\s*"([a-zA-Z0-9_-]{11})"',
                r'"video_id"\s*:\s*"([a-zA-Z0-9_-]{11})"',                r'watch\?v=([a-zA-Z0-9_-]{11})',
                r'/live/([a-zA-Z0-9_-]{11})'
            )

            ids_pagina = []
            for padrao in padroes:
                ids_pagina.extend(re.findall(padrao, html))

            # Mantém a ordem em que os IDs aparecem no HTML.            for candidato in ids_pagina:
                if candidato not in encontrados:
                    encontrados.append(candidato)

        except Exception as erro:
            print(f"[LIVE] Erro ao consultar {sufixo}: {erro}")

        # Se /live trouxe um ID válido, não precisamos consultar
        # as páginas de vídeos antigos.
        if sufixo == "/live":
            for candidato in encontrados:
                if live_esta_ativa(candidato):
                    return candidato

    # Em /streams e /videos podem aparecer vídeos antigos.
    # Testa os candidatos até encontrar uma transmissão ativa.
    for candidato in encontrados:
        if live_esta_ativa(candidato):
            return candidato

    return None


def selecionar_live_inicial():

    global LIVE_ID

    print()
    print("[LIVE] Informe o ID/link de uma live.")
    print("[LIVE] Pressione ENTER para detectar automaticamente a live mais recente.")

    try:
        entrada = input("ID ou link (ENTER = automático): ").strip()
    except (EOFError, KeyboardInterrupt):
        entrada = ""

    if entrada:
        live_id = extrair_live_id(entrada)

        if not live_id:
            raise ValueError("O ID/link informado não é válido.")

        if not live_esta_ativa(live_id):
            raise RuntimeError(
                "O ID informado não parece ser uma transmissão ao vivo ativa."
            )

        LIVE_ID = live_id
        print(f"[LIVE] Live manual selecionada: {LIVE_ID}")
        return False

    print(f"[LIVE] Modo automático ativado para {CANAL}.")
    while rodando and not LIVE_ID:
        live_id = pegar_live_id(CANAL)
        if live_id:
            LIVE_ID = live_id
            print(f"[LIVE] Live encontrada: {LIVE_ID}")
            break

        print(
            f"[LIVE] Nenhuma transmissão ativa encontrada. "
            f"Nova tentativa em {INTERVALO_VERIFICACAO_LIVE}s..."
        )
        time.sleep(INTERVALO_VERIFICACAO_LIVE)

    return True


def monitorar_live(modo_automatico=False):

    global rodando, LIVE_ID

    while rodando:

        chat = None
        id_conectado = LIVE_ID

        try:

            print(f"[LIVE] Conectando ao chat: {LIVE_ID}")

            chat = pytchat.create(
                video_id=LIVE_ID,
                interruptable=False
            )

            if not chat.is_alive():

                print("[LIVE] A transmissão não está disponível.")
                time.sleep(5)
                continue

            print("[LIVE] Conectado!")

            thread_chat = threading.Thread(
                target=ler_chat,
                args=(chat,),
                daemon=True
            )
            thread_chat.start()

            proxima_verificacao = time.time() + INTERVALO_VERIFICACAO_LIVE

            while rodando and chat.is_alive():

                # No modo automático, acompanha o canal e troca
                # de transmissão quando um novo ID for detectado.
                if modo_automatico and time.time() >= proxima_verificacao:

                    proxima_verificacao = (
                        time.time() + INTERVALO_VERIFICACAO_LIVE
                    )

                    novo_id = pegar_live_id(CANAL)

                    if novo_id and novo_id != LIVE_ID:
                        print(
                            f"[LIVE] Nova transmissão detectada: {novo_id}"
                        )
                        LIVE_ID = novo_id
                        break

                try:
                    while True:
                        mensagem = fila_mensagens.get_nowait()

                        if mensagem is None:
                            rodando = False
                            break

                        enviar_mensagem(mensagem)

                except queue.Empty:
                    pass

                time.sleep(0.05)

            print("[LIVE] Conexão encerrada.")

        except Exception as erro:
            print("[LIVE] Erro:", erro)

        finally:
            if chat:
                try:
                    chat.terminate()
                except Exception:
                    pass

        if rodando:
            if modo_automatico:
                # Se a transmissão terminou, procura outra. Se o ID mudou
                # durante o monitoramento, atualiza o navegador também.
                novo_id = pegar_live_id(CANAL)

                if novo_id and novo_id != id_conectado:
                    LIVE_ID = novo_id
                    print(f"[LIVE] Mudando para a nova transmissão: {LIVE_ID}")

                    try:
                        abrir_chat(LIVE_ID)
                    except Exception as erro:
                        print("[CHAT] Erro ao abrir o chat da nova live:", erro)
                        time.sleep(5)

                elif not novo_id:
                    print(
                        "[LIVE] Nenhuma live ativa encontrada. "
                        f"Nova busca em {INTERVALO_VERIFICACAO_LIVE}s..."
                    )
                    time.sleep(INTERVALO_VERIFICACAO_LIVE)

                else:
                    print(
                        "[LIVE] A live atual continua sendo a mais recente. "
                        f"Nova verificação em {INTERVALO_VERIFICACAO_LIVE}s..."
                    )
                    time.sleep(INTERVALO_VERIFICACAO_LIVE)

            else:
                print("[LIVE] Tentando reconectar em 10 segundos...")
                time.sleep(10)


# ============================================================
# ENCERRAR
# ============================================================

def encerrar():

    global rodando
    global driver

    rodando = False

    fila_eventos_chat.put(None)
    fila_recompensas.put(None)

    print("\n[ENCERRANDO] Finalizando bot...")

    if driver:

        try:
            driver.quit()
        except Exception:
            pass

        driver = None

    print("[ENCERRANDO] Bot finalizado.")


# ============================================================
# MAIN
# ============================================================

def main():

    print("======================================")
    print("       YOUTUBE CHAT BOT")
    print("======================================")

    print("[CONFIG] Canal:", CANAL)
    print("[CONFIG] Recompensa por participação:", RECOMPENSA, "moeda(s) a cada", INTERVALO_RECOMPENSA, "segundos")
    print("[CONFIG] Bônus:", BONUS_POR_MENSAGENS, "moedas a cada", MENSAGENS_PARA_BONUS, "mensagens válidas")
    print("[CONFIG] Limite:", LIMITE_RECOMPENSA_HORA, "moedas por jogador/hora")

    print("[CONFIG] Perfil:", PERFIL)

    try:

        modo_automatico = selecionar_live_inicial()
        print("[CONFIG] Live selecionada:", LIVE_ID)

        iniciar_flask()

        abrir_navegador()

        abrir_chat(LIVE_ID)

        thread_processador = threading.Thread(
            target=processar_eventos_chat,
            daemon=True
        )

        thread_processador.start()

        thread_recompensas = threading.Thread(
            target=processar_recompensas,
            daemon=True
        )

        thread_recompensas.start()

        thread_terminal = threading.Thread(
            target=receber_mensagens,
            daemon=True
        )

        thread_terminal.start()

        monitorar_live(modo_automatico)

    except KeyboardInterrupt:

        print("\n[MAIN] Interrompido pelo usuário.")

    except Exception as erro:

        print("[MAIN] Erro:", erro)

    finally:

        encerrar()


if __name__ == "__main__":
    main()