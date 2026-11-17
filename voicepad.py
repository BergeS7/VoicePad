"""
VoicePad
--------
Ctrl+0 liga/desliga o "modo VoicePad".
Com o modo ATIVO, as teclas do teclado numérico (0-9) tocam os sons
configurados e NÃO digitam números. A tecla "." (ponto/Del) do numpad para tudo.
Com o modo INATIVO, o teclado numérico funciona normalmente.

Fechar a janela (X) só esconde o programa: ele continua na bandeja do Windows,
perto do relógio. Para sair de vez, use "Sair" no menu do ícone.

Dependências:  pip install keyboard pygame-ce pystray pillow
"""

import array
import ctypes
import json
import multiprocessing as mp
import os
import queue
import re
import shutil
import sys
import tempfile
import tkinter as tk
import wave
import winreg
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import keyboard
import pystray
from PIL import Image, ImageDraw, ImageEnhance, ImageTk

os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"
import pygame  # noqa: E402

# No .exe (PyInstaller), config e sons ficam ao lado do executável e a logo vem embutida.
CONGELADO = getattr(sys, "frozen", False)
BASE = Path(sys.executable).resolve().parent if CONGELADO else Path(__file__).resolve().parent
RECURSOS = Path(getattr(sys, "_MEIPASS", BASE))
CONFIG = BASE / "config.json"
SONS_DIR = BASE / "sons"
LOGO_PNG = RECURSOS / "assets" / "logo.png"
LOGO_ICO = RECURSOS / "assets" / "voicepad.ico"

CHAVE_RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"
NOME_APP = "VoicePad"


def comando_inicializacao():
    """Comando gravado no registro para abrir o VoicePad com o Windows (já na bandeja)."""
    if CONGELADO:
        return f'"{Path(sys.executable).resolve()}" --bandeja'
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    if pythonw.exists():
        exe = pythonw
    return f'"{exe}" "{Path(__file__).resolve()}" --bandeja'


def inicia_com_windows():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, CHAVE_RUN) as k:
            winreg.QueryValueEx(k, NOME_APP)
            return True
    except OSError:
        return False


def definir_inicio_com_windows(ligar):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, CHAVE_RUN, 0, winreg.KEY_SET_VALUE) as k:
        if ligar:
            winreg.SetValueEx(k, NOME_APP, 0, winreg.REG_SZ, comando_inicializacao())
        else:
            try:
                winreg.DeleteValue(k, NOME_APP)
            except FileNotFoundError:
                pass

# Scan codes do teclado numérico (iguais com NumLock ligado ou desligado).
NUMPAD_SC = {82: "0", 79: "1", 80: "2", 81: "3", 75: "4",
             76: "5", 77: "6", 71: "7", 72: "8", 73: "9"}
SC_NUMPAD_DOT = 83
SC_ZERO_LINHA = 11  # o "0" da fileira de cima

NENHUMA = "Nenhuma (desligada)"
TIPOS_AUDIO = [("Áudio", "*.mp3 *.wav *.ogg *.flac"), ("Todos os arquivos", "*.*")]

# Cores
BG = "#16161d"
PAD = "#24242f"
PAD_HOVER = "#2f2f3d"
PAD_VAZIO = "#1d1d26"
TEXTO = "#ececf1"
TEXTO_FRACO = "#8b8b9a"
VERDE = "#2ecc71"
VERMELHO = "#e74c3c"
DESTAQUE = "#f1c40f"


class Audio:
    """Toca os sons com pygame (permite sobrepor e escolher a saída de áudio)."""

    def __init__(self, dispositivo=None, reserva=True):
        self.cache = {}
        self.dispositivo = None
        self.reserva = reserva  # se a saída escolhida não existir, usa a padrão do Windows?
        self._iniciar(dispositivo)

    def _iniciar(self, dispositivo):
        if pygame.mixer.get_init():
            pygame.mixer.quit()
        self.cache.clear()
        try:
            if dispositivo:
                pygame.mixer.init(devicename=dispositivo)
            else:
                pygame.mixer.init()
            self.dispositivo = dispositivo
        except pygame.error:
            if not self.reserva:
                raise
            pygame.mixer.init()
            self.dispositivo = None
        pygame.mixer.set_num_channels(32)

    def abriu(self, pedido):
        """True se a saída pedida foi aberta (e não a padrão no lugar dela)."""
        return not pedido or self.dispositivo == pedido

    def trocar_dispositivo(self, dispositivo):
        self._iniciar(dispositivo or None)

    @staticmethod
    def listar_dispositivos():
        try:
            from pygame._sdl2 import audio as sdl2_audio
            was_init = pygame.mixer.get_init()
            if not was_init:
                pygame.mixer.init()
            return list(sdl2_audio.get_audio_device_names(False))
        except Exception:
            return []

    def tocar(self, caminho, volume, sobrepor):
        som = self.cache.get(caminho)
        if som is None:
            som = pygame.mixer.Sound(caminho)
            self.cache[caminho] = som
        if not sobrepor:
            pygame.mixer.stop()
        som.set_volume(volume)
        som.play()

    def esquecer(self, caminho):
        self.cache.pop(caminho, None)

    @staticmethod
    def parar():
        pygame.mixer.stop()


def _trabalhador_audio(fila_cmd, dispositivo):
    """Processo separado que toca na segunda saída (o pygame só abre uma saída por processo)."""
    try:
        # Sem reserva: se os fones sumirem, não toca nas caixas (o som sairia dobrado).
        audio = Audio(dispositivo, reserva=False)
    except pygame.error:
        audio = None
    while True:
        cmd = fila_cmd.get()
        if cmd[0] == "sair":
            break
        if audio is None:
            continue
        try:
            if cmd[0] == "tocar":
                audio.tocar(*cmd[1:])
            elif cmd[0] == "parar":
                audio.parar()
            elif cmd[0] == "esquecer":
                audio.esquecer(cmd[1])
        except Exception:
            pass
    if pygame.mixer.get_init():
        pygame.mixer.quit()


class SaidaExtra:
    """Controla o processo da segunda saída de áudio (ex.: seus fones)."""

    def __init__(self):
        self.proc = None
        self.fila = None
        self.desejado = None  # saída escolhida, mesmo que esteja desconectada agora

    def definir(self, dispositivo):
        """Abre a segunda saída. Retorna False se ela não estiver conectada agora."""
        self.fechar()
        self.desejado = dispositivo or None
        if not self.desejado:
            return True
        if self.desejado not in Audio.listar_dispositivos():
            return False
        self.fila = mp.Queue()
        self.proc = mp.Process(target=_trabalhador_audio, args=(self.fila, self.desejado), daemon=True)
        self.proc.start()
        return True

    def pendente(self):
        """Há uma saída escolhida que ainda não pôde ser aberta?"""
        return self.desejado is not None and self.proc is None

    def enviar(self, *cmd):
        if self.fila is not None:
            self.fila.put(cmd)

    def fechar(self):
        if self.proc is not None:
            self.fila.put(("sair",))
            self.proc.join(1.5)
            if self.proc.is_alive():
                self.proc.terminate()
        self.proc = None
        self.fila = None


TAXA_GRAVACAO = 44100
MAX_SEGUNDOS = 60


class Gravador:
    """Grava do microfone pelo SDL do pygame-ce (16 bits, mono, 44,1 kHz)."""

    def __init__(self):
        self.dev = None
        self.blocos = []
        self.nivel = 0      # pico do último bloco (0 a 32768), para o medidor
        self.pico_total = 0

    @staticmethod
    def listar():
        try:
            from pygame._sdl2.audio import get_audio_device_names
            if not pygame.mixer.get_init():
                pygame.mixer.init()
            return list(get_audio_device_names(True))
        except Exception:
            return []

    def iniciar(self, microfone):
        from pygame._sdl2.audio import AUDIO_S16, AudioDevice
        self.blocos, self.nivel, self.pico_total = [], 0, 0
        self.dev = AudioDevice(devicename=microfone, iscapture=True, frequency=TAXA_GRAVACAO,
                               audioformat=AUDIO_S16, numchannels=1, chunksize=1024,
                               allowed_changes=0, callback=self._receber)
        self.dev.pause(0)

    def _receber(self, _dev, memoria):  # roda na thread de áudio do SDL
        dados = bytes(memoria)
        self.blocos.append(dados)
        amostras = array.array("h", dados)
        if amostras:
            self.nivel = max(-min(amostras), max(amostras))
            self.pico_total = max(self.pico_total, self.nivel)

    def parar(self):
        if self.dev is not None:
            self.dev.pause(1)
            self.dev.close()
            self.dev = None
        return b"".join(self.blocos)

    def segundos(self):
        return sum(len(b) for b in self.blocos) / 2 / TAXA_GRAVACAO

    @staticmethod
    def cortar_silencio(dados, limiar=700, folga=0.12):
        """Tira o silêncio do começo e do fim, deixando uma pequena folga."""
        a = array.array("h", dados)
        altos = [i for i in range(0, len(a), 64) if abs(a[i]) > limiar]
        if not altos:
            return dados
        f = int(folga * TAXA_GRAVACAO)
        ini, fim = max(0, altos[0] - f), min(len(a), altos[-1] + f)
        return a[ini:fim].tobytes()

    @staticmethod
    def salvar_wav(dados, caminho):
        with wave.open(str(caminho), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(TAXA_GRAVACAO)
            w.writeframes(dados)


class JanelaGravar(tk.Toplevel):
    """Janela para gravar um som pelo microfone e salvar numa tecla."""

    def __init__(self, app, tecla=None):
        super().__init__(app.root)
        self.app = app
        self.grav = Gravador()
        self.gravando = False
        self.dados = b""
        self.previa = Path(tempfile.gettempdir()) / "voicepad_previa.wav"

        self.title("Gravar som — VoicePad")
        self.configure(bg=BG, padx=18, pady=14)
        self.resizable(False, False)
        self.transient(app.root)
        self.protocol("WM_DELETE_WINDOW", self.fechar)

        def rot(txt):
            return tk.Label(self, text=txt, bg=BG, fg=TEXTO_FRACO, font=("Segoe UI", 9))

        rot("Microfone").grid(row=0, column=0, sticky="w")
        mics = Gravador.listar()
        self.var_mic = tk.StringVar(value=mics[0] if mics else "")
        ttk.Combobox(self, textvariable=self.var_mic, values=mics, state="readonly", width=46).grid(
            row=1, column=0, columnspan=3, sticky="we", pady=(2, 10))

        self.btn = tk.Button(self, text="●  Gravar", command=self.alternar, bg=VERMELHO, fg="white",
                             relief="flat", font=("Segoe UI", 12, "bold"), pady=6,
                             activebackground="#c0392b", activeforeground="white")
        self.btn.grid(row=2, column=0, columnspan=3, sticky="we")

        self.lbl_tempo = tk.Label(self, text="0,0 s", bg=BG, fg=TEXTO, font=("Segoe UI", 10))
        self.lbl_tempo.grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.medidor = tk.Canvas(self, width=260, height=10, bg=PAD, highlightthickness=0)
        self.medidor.grid(row=3, column=1, columnspan=2, sticky="e", pady=(6, 0))
        self.barra = self.medidor.create_rectangle(0, 0, 0, 10, fill=VERDE, width=0)

        self.lbl_aviso = tk.Label(self, text=f"Máximo de {MAX_SEGUNDOS} segundos.", bg=BG,
                                  fg=TEXTO_FRACO, font=("Segoe UI", 8), justify="left", wraplength=340)
        self.lbl_aviso.grid(row=4, column=0, columnspan=3, sticky="w", pady=(4, 10))

        # --- salvar
        self.salvar_frame = tk.Frame(self, bg=BG)
        self.salvar_frame.grid(row=5, column=0, columnspan=3, sticky="we")
        sf = self.salvar_frame
        tk.Label(sf, text="Nome", bg=BG, fg=TEXTO_FRACO, font=("Segoe UI", 9)).grid(row=0, column=0, sticky="w")
        self.var_nome = tk.StringVar(value=datetime.now().strftime("gravacao_%Y%m%d_%H%M%S"))
        tk.Entry(sf, textvariable=self.var_nome, bg=PAD, fg=TEXTO, insertbackground=TEXTO,
                 relief="flat", width=30).grid(row=0, column=1, sticky="we", padx=(8, 0))
        tk.Label(sf, text="Tecla", bg=BG, fg=TEXTO_FRACO, font=("Segoe UI", 9)).grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.var_tecla = tk.StringVar(value=tecla or self._primeira_tecla_vazia())
        ttk.Combobox(sf, textvariable=self.var_tecla, values=[str(i) for i in range(10)],
                     state="readonly", width=5).grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(6, 0))
        self.var_cortar = tk.BooleanVar(value=True)
        tk.Checkbutton(sf, text="Cortar silêncio do começo e do fim", variable=self.var_cortar,
                       bg=BG, fg=TEXTO, selectcolor=PAD, activebackground=BG, activeforeground=TEXTO).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))

        acoes = tk.Frame(sf, bg=BG)
        acoes.grid(row=3, column=0, columnspan=2, sticky="we", pady=(10, 0))
        acoes.columnconfigure((0, 1), weight=1, uniform="a")
        self.btn_ouvir = tk.Button(acoes, text="▶  Ouvir", command=self.ouvir, bg=PAD, fg=TEXTO,
                                   relief="flat", font=("Segoe UI", 9, "bold"), activebackground=PAD_HOVER)
        self.btn_ouvir.grid(row=0, column=0, sticky="we", padx=(0, 4))
        self.btn_salvar = tk.Button(acoes, text="✔  Salvar na tecla", command=self.salvar, bg=VERDE, fg=BG,
                                    relief="flat", font=("Segoe UI", 9, "bold"), activebackground="#27ae60")
        self.btn_salvar.grid(row=0, column=1, sticky="we", padx=(4, 0))
        self._habilitar_salvar(False)
        if not mics:
            self.btn.configure(state="disabled")
            self.lbl_aviso.configure(text="Nenhum microfone encontrado.", fg=VERMELHO)

    def _primeira_tecla_vazia(self):
        for t in "1234567890":
            if not self.app._caminho_slot(t):
                return t
        return "1"

    def _habilitar_salvar(self, sim):
        apagado = {"bg": "#2a2a34", "state": "disabled", "disabledforeground": "#5a5a68"}
        self.btn_ouvir.configure(**({"bg": PAD, "state": "normal"} if sim else apagado))
        self.btn_salvar.configure(**({"bg": VERDE, "state": "normal"} if sim else apagado))

    def alternar(self):
        if self.gravando:
            self.parar()
        else:
            self.iniciar()

    def iniciar(self):
        try:
            self.grav.iniciar(self.var_mic.get())
        except Exception as err:
            messagebox.showerror("Gravar som", f"Não consegui abrir o microfone:\n{err}", parent=self)
            return
        self.gravando = True
        self._habilitar_salvar(False)
        self.btn.configure(text="■  Parar gravação", bg="#3a3a48", activebackground="#4a4a5a")
        self.lbl_aviso.configure(text="Gravando… fale agora.", fg=TEXTO)
        self._atualizar()

    def _atualizar(self):
        if not self.gravando:
            return
        seg = self.grav.segundos()
        self.lbl_tempo.configure(text=f"{seg:.1f} s".replace(".", ","))
        nivel = min(1.0, self.grav.nivel / 20000)
        self.medidor.coords(self.barra, 0, 0, int(260 * nivel), 10)
        self.medidor.itemconfigure(self.barra, fill=VERMELHO if nivel > 0.9 else VERDE)
        if seg >= MAX_SEGUNDOS:
            self.parar()
            return
        self.after(50, self._atualizar)

    def parar(self):
        self.gravando = False
        self.dados = self.grav.parar()
        self.medidor.coords(self.barra, 0, 0, 0, 10)
        self.btn.configure(text="●  Gravar de novo", bg=VERMELHO, activebackground="#c0392b")
        if not self.dados:
            self.lbl_aviso.configure(text="Nada foi gravado.", fg=VERMELHO)
            return
        if self.grav.pico_total < 300:
            self.lbl_aviso.configure(
                fg=DESTAQUE,
                text="A gravação ficou muda. Veja se o microfone certo está escolhido e se o Windows "
                     "permite o acesso: Configurações > Privacidade > Microfone > "
                     "\"Permitir que aplicativos da área de trabalho acessem o microfone\".")
        else:
            self.lbl_aviso.configure(text="Pronto! Ouça e salve numa tecla.", fg=VERDE)
        self._habilitar_salvar(True)

    def _dados_finais(self):
        return Gravador.cortar_silencio(self.dados) if self.var_cortar.get() else self.dados

    def ouvir(self):
        Gravador.salvar_wav(self._dados_finais(), self.previa)
        vol = self.app.var_volume.get() / 100
        # Prefere a saída "ouvir também" (seus fones) para a prévia não ir para a call.
        if self.app.extra.proc is not None:
            self.app.extra.enviar("esquecer", str(self.previa))
            self.app.extra.enviar("tocar", str(self.previa), self.app.var_volume2.get() / 100, False)
        else:
            self.app.audio.esquecer(str(self.previa))
            self.app.audio.tocar(str(self.previa), vol, False)

    def salvar(self):
        tecla = self.var_tecla.get()
        atual = self.app._caminho_slot(tecla)
        if atual and not messagebox.askyesno(
                "Gravar som", f"A tecla {tecla} já tem \"{atual.stem}\".\nSubstituir?", parent=self):
            return
        nome = re.sub(r'[\\/:*?"<>|]+', "_", self.var_nome.get()).strip() or "gravacao"
        SONS_DIR.mkdir(exist_ok=True)
        destino, n = SONS_DIR / f"{nome}.wav", 1
        while destino.exists():
            destino, n = SONS_DIR / f"{nome}_{n}.wav", n + 1
        Gravador.salvar_wav(self._dados_finais(), destino)
        self.app.atribuir(tecla, destino)
        self.app._toast(f"🎙  Gravação salva na tecla {tecla}", "#7c3aed")
        self.fechar()

    def fechar(self):
        if self.gravando:
            self.gravando = False
            self.grav.parar()
        self.destroy()


class App:
    def __init__(self, root):
        self.root = root
        self.fila = queue.Queue()
        self.ativo = False
        self.engolidas = set()  # teclas cujo "pressionar" foi bloqueado
        self.toast_win = None
        self.toast_job = None

        self.cfg = self._carregar_config()
        avisos = []
        pedido = self.cfg.get("dispositivo") or None
        self.audio = Audio(pedido)
        if not self.audio.abriu(pedido):
            avisos.append(f"Saída principal não encontrada:\n{pedido}\nUsando a padrão do Windows.")
        self.extra = SaidaExtra()
        if not self.extra.definir(self.cfg.get("dispositivo2") or None):
            avisos.append(f"Não achei \"{self.cfg['dispositivo2']}\".\nConecte e ela volta a tocar sozinha.")

        self.icone = None
        self.avisou_bandeja = False
        self.janela_gravar = None

        self._montar_tela()
        self._iniciar_bandeja()
        self._atualizar_status()

        keyboard.hook(self._hook_teclado, suppress=True)
        self.root.after(15, self._processar_fila)
        self.root.protocol("WM_DELETE_WINDOW", self._esconder)
        if avisos:
            self.root.after(600, lambda: self._toast("⚠  " + "\n\n".join(avisos), "#b9770e", 6000))

    # ------------------------------------------------------------ config
    def _carregar_config(self):
        padrao = {"slots": {}, "volume": 0.8, "sobrepor": True, "dispositivo": "",
                  "dispositivo2": "", "volume2": 0.6}
        try:
            dados = json.loads(CONFIG.read_text(encoding="utf-8"))
            padrao.update(dados)
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        return padrao

    def _salvar_config(self):
        self.cfg["volume"] = self.var_volume.get() / 100
        self.cfg["sobrepor"] = self.var_sobrepor.get()
        self.cfg["dispositivo"] = self.var_dispositivo.get() if self.var_dispositivo.get() != "Padrão do Windows" else ""
        self.cfg["volume2"] = self.var_volume2.get() / 100
        self.cfg["dispositivo2"] = self.var_dispositivo2.get() if self.var_dispositivo2.get() != NENHUMA else ""
        CONFIG.write_text(json.dumps(self.cfg, indent=2, ensure_ascii=False), encoding="utf-8")

    def _caminho_slot(self, tecla):
        rel = self.cfg["slots"].get(tecla)
        if not rel:
            return None
        p = Path(rel)
        return p if p.is_absolute() else BASE / p

    # ------------------------------------------------------------ teclado
    def _hook_teclado(self, e):
        """Roda na thread do hook: precisa ser rápido. Retornar False bloqueia a tecla."""
        sc = e.scan_code
        if e.event_type == keyboard.KEY_DOWN:
            e_zero = (sc == SC_ZERO_LINHA and not e.is_keypad) or (sc == 82 and e.is_keypad)
            if e_zero and keyboard.is_pressed("ctrl"):
                if sc not in self.engolidas:
                    self.fila.put(("alternar", None))
                self.engolidas.add(sc)
                return False
            if self.ativo and e.is_keypad and (sc in NUMPAD_SC or sc == SC_NUMPAD_DOT):
                if sc not in self.engolidas:  # ignora repetição de tecla segurada
                    self.fila.put(("tecla", NUMPAD_SC.get(sc, "parar")))
                self.engolidas.add(sc)
                return False
            return True
        # KEY_UP
        if sc in self.engolidas:
            self.engolidas.discard(sc)
            return False
        return True

    def _processar_fila(self):
        try:
            while True:
                acao, valor = self.fila.get_nowait()
                if acao == "alternar":
                    self.alternar()
                elif acao == "mostrar":
                    self._mostrar()
                elif acao == "parar":
                    self.parar_tudo()
                elif acao == "iniciar":
                    self._definir_iniciar(not inicia_com_windows())
                elif acao == "sair":
                    self._sair()
                    return
                elif acao == "tecla":
                    if valor == "parar":
                        self.parar_tudo()
                        self._piscar(".")
                    else:
                        self.tocar_slot(valor)
        except queue.Empty:
            pass
        self.root.after(15, self._processar_fila)

    # ------------------------------------------------------------ ações
    def alternar(self):
        self.ativo = not self.ativo
        self._atualizar_status()
        self._toast("🔊  VoicePad ATIVADO" if self.ativo else "⌨️  VoicePad DESATIVADO",
                    VERDE if self.ativo else VERMELHO)

    def tocar_slot(self, tecla):
        caminho = self._caminho_slot(tecla)
        if not caminho:
            return
        if not caminho.exists():
            self._toast(f"Arquivo sumiu: {caminho.name}", VERMELHO)
            return
        try:
            self.audio.tocar(str(caminho), self.var_volume.get() / 100, self.var_sobrepor.get())
            if self.extra.pendente():  # os fones foram conectados depois? tenta abrir de novo
                self.extra.definir(self.extra.desejado)
            self.extra.enviar("tocar", str(caminho), self.var_volume2.get() / 100, self.var_sobrepor.get())
            self._piscar(tecla)
        except pygame.error as err:
            self._toast(f"Erro ao tocar: {err}", VERMELHO)

    def parar_tudo(self):
        self.audio.parar()
        self.extra.enviar("parar")

    def _esquecer(self, caminho):
        self.audio.esquecer(str(caminho))
        self.extra.enviar("esquecer", str(caminho))

    def escolher_som(self, tecla):
        arquivo = filedialog.askopenfilename(title=f"Som para a tecla {tecla}", filetypes=TIPOS_AUDIO)
        if not arquivo:
            return
        origem = Path(arquivo)
        SONS_DIR.mkdir(exist_ok=True)
        destino = SONS_DIR / origem.name
        if origem.resolve() != destino.resolve():
            n = 1
            while destino.exists():
                destino = SONS_DIR / f"{origem.stem}_{n}{origem.suffix}"
                n += 1
            shutil.copy2(origem, destino)
        self.atribuir(tecla, destino)

    def atribuir(self, tecla, destino):
        """Liga um arquivo (já dentro de sons/) a uma tecla."""
        antigo = self._caminho_slot(tecla)
        if antigo:
            self._esquecer(antigo)
        self.cfg["slots"][tecla] = str(Path(destino).relative_to(BASE))
        self._salvar_config()
        self._atualizar_pad(tecla)

    def abrir_gravador(self, tecla=None):
        if self.janela_gravar is not None and self.janela_gravar.winfo_exists():
            self.janela_gravar.lift()
            self.janela_gravar.focus_force()
            return
        self._mostrar()
        self.janela_gravar = JanelaGravar(self, tecla)

    def remover_som(self, tecla):
        antigo = self._caminho_slot(tecla)
        if antigo:
            self._esquecer(antigo)
        self.cfg["slots"].pop(tecla, None)
        self._salvar_config()
        self._atualizar_pad(tecla)

    # ------------------------------------------------------------ interface
    def _montar_tela(self):
        r = self.root
        r.title("VoicePad")
        r.configure(bg=BG)
        r.resizable(False, False)
        if LOGO_ICO.exists():
            r.iconbitmap(default=str(LOGO_ICO))

        topo = tk.Frame(r, bg=BG)
        topo.pack(fill="x", padx=16, pady=(14, 6))
        if LOGO_PNG.exists():
            self._logo_tk = ImageTk.PhotoImage(Image.open(LOGO_PNG).resize((34, 34), Image.LANCZOS))
            tk.Label(topo, image=self._logo_tk, bg=BG).pack(side="left", padx=(0, 8))
        tk.Label(topo, text="VoicePad", bg=BG, fg=TEXTO,
                 font=("Segoe UI", 16, "bold")).pack(side="left")
        self.lbl_status = tk.Label(topo, text="", font=("Segoe UI", 10, "bold"),
                                   padx=10, pady=3, cursor="hand2")
        self.lbl_status.pack(side="right")
        self.lbl_status.bind("<Button-1>", lambda _e: self.alternar())

        tk.Label(r, text="Ctrl + 0 liga/desliga  •  clique = tocar  •  botão direito = trocar/remover",
                 bg=BG, fg=TEXTO_FRACO, font=("Segoe UI", 9)).pack(padx=16, anchor="w")

        grade = tk.Frame(r, bg=BG)
        grade.pack(padx=16, pady=12)
        layout = [["7", "8", "9"], ["4", "5", "6"], ["1", "2", "3"]]
        self.pads = {}
        for i, linha in enumerate(layout):
            for j, tecla in enumerate(linha):
                self._criar_pad(grade, tecla, i, j)
        self._criar_pad(grade, "0", 3, 0, colspan=2)
        self._criar_pad(grade, ".", 3, 2)

        # Controles
        ctrl = tk.Frame(r, bg=BG)
        ctrl.pack(fill="x", padx=16, pady=(0, 14))

        estilo = ttk.Style()
        estilo.theme_use("clam")
        estilo.configure("TCombobox", fieldbackground=PAD, background=PAD, foreground=TEXTO,
                         arrowcolor=TEXTO, bordercolor=PAD, lightcolor=PAD, darkcolor=PAD)
        estilo.map("TCombobox",
                   fieldbackground=[("readonly", PAD)], foreground=[("readonly", TEXTO)],
                   selectbackground=[("readonly", PAD)], selectforeground=[("readonly", TEXTO)],
                   background=[("active", PAD_HOVER), ("readonly", PAD)])
        r.option_add("*TCombobox*Listbox.background", PAD)
        r.option_add("*TCombobox*Listbox.foreground", TEXTO)
        r.option_add("*TCombobox*Listbox.selectBackground", "#7c3aed")
        r.option_add("*TCombobox*Listbox.selectForeground", "white")

        dispositivos = Audio.listar_dispositivos()

        def rotulo(texto, linha, topo=0):
            tk.Label(ctrl, text=texto, bg=BG, fg=TEXTO_FRACO, font=("Segoe UI", 9)).grid(
                row=linha, column=0, sticky="w", pady=(topo, 0), padx=(0, 10))

        def barra_volume(var, linha):
            tk.Scale(ctrl, from_=0, to=100, orient="horizontal", variable=var, showvalue=True,
                     bg=BG, fg=TEXTO, troughcolor=PAD, highlightthickness=0, length=280,
                     command=lambda _v: self._salvar_config()).grid(row=linha, column=1, sticky="w")

        # Saída 1: principal (na call, escolha CABLE Input)
        rotulo("Saída principal", 0)
        self.var_dispositivo = tk.StringVar(value=self.cfg.get("dispositivo") or "Padrão do Windows")
        combo = ttk.Combobox(ctrl, textvariable=self.var_dispositivo, state="readonly", width=44,
                             values=["Padrão do Windows"] + dispositivos)
        combo.grid(row=0, column=1, sticky="w")
        combo.bind("<<ComboboxSelected>>", self._trocar_dispositivo)
        rotulo("Volume", 1)
        self.var_volume = tk.IntVar(value=int(self.cfg["volume"] * 100))
        barra_volume(self.var_volume, 1)

        # Saída 2: para você ouvir (seus fones)
        rotulo("Ouvir também em", 2, topo=8)
        self.var_dispositivo2 = tk.StringVar(value=self.cfg.get("dispositivo2") or NENHUMA)
        combo2 = ttk.Combobox(ctrl, textvariable=self.var_dispositivo2, state="readonly", width=44,
                              values=[NENHUMA] + dispositivos)
        combo2.grid(row=2, column=1, sticky="w", pady=(8, 0))
        combo2.bind("<<ComboboxSelected>>", self._trocar_dispositivo2)
        rotulo("Volume", 3)
        self.var_volume2 = tk.IntVar(value=int(self.cfg["volume2"] * 100))
        barra_volume(self.var_volume2, 3)

        tk.Label(ctrl, text="Em call: principal = CABLE Input  •  ouvir também = seus fones",
                 bg=BG, fg=TEXTO_FRACO, font=("Segoe UI", 8)).grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))

        self.var_sobrepor = tk.BooleanVar(value=self.cfg["sobrepor"])
        tk.Checkbutton(ctrl, text="Sobrepor sons (tocar vários ao mesmo tempo)", variable=self.var_sobrepor,
                       bg=BG, fg=TEXTO, selectcolor=PAD, activebackground=BG, activeforeground=TEXTO,
                       command=self._salvar_config).grid(row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))

        self.var_iniciar = tk.BooleanVar(value=inicia_com_windows())
        tk.Checkbutton(ctrl, text="Iniciar com o Windows (abre direto na bandeja)", variable=self.var_iniciar,
                       bg=BG, fg=TEXTO, selectcolor=PAD, activebackground=BG, activeforeground=TEXTO,
                       command=lambda: self._definir_iniciar(self.var_iniciar.get())).grid(
            row=6, column=0, columnspan=2, sticky="w")

        botoes = tk.Frame(ctrl, bg=BG)
        botoes.grid(row=7, column=0, columnspan=2, sticky="we", pady=(10, 0))
        botoes.columnconfigure((0, 1), weight=1, uniform="b")
        tk.Button(botoes, text="🎙  Gravar som", command=self.abrir_gravador, bg="#7c3aed", fg="white",
                  relief="flat", font=("Segoe UI", 9, "bold"), padx=10,
                  activebackground="#6d28d9", activeforeground="white").grid(row=0, column=0, sticky="we", padx=(0, 4))
        tk.Button(botoes, text="■  Parar tudo", command=self.parar_tudo, bg=VERMELHO, fg="white",
                  relief="flat", font=("Segoe UI", 9, "bold"), padx=10,
                  activebackground="#c0392b", activeforeground="white").grid(row=0, column=1, sticky="we", padx=(4, 0))

    def _definir_iniciar(self, ligar):
        try:
            definir_inicio_com_windows(ligar)
        except OSError as err:
            messagebox.showerror(NOME_APP, f"Não consegui alterar a inicialização:\n{err}")
        self.var_iniciar.set(inicia_com_windows())
        if self.icone is not None:
            self.icone.update_menu()

    def _criar_pad(self, pai, tecla, linha, coluna, colspan=1):
        largura = 120 * colspan + 8 * (colspan - 1)
        f = tk.Frame(pai, bg=PAD, width=largura, height=86, cursor="hand2")
        f.grid(row=linha, column=coluna, columnspan=colspan, padx=4, pady=4)
        f.grid_propagate(False)
        f.pack_propagate(False)
        lbl_tecla = tk.Label(f, text=tecla, bg=PAD, fg=TEXTO, font=("Segoe UI", 20, "bold"))
        lbl_tecla.pack(pady=(10, 0))
        lbl_nome = tk.Label(f, text="", bg=PAD, fg=TEXTO_FRACO, font=("Segoe UI", 8),
                            wraplength=largura - 12)
        lbl_nome.pack()
        self.pads[tecla] = (f, lbl_tecla, lbl_nome)

        def clique(_e):
            if tecla == ".":
                self.parar_tudo()
            elif self._caminho_slot(tecla):
                self.tocar_slot(tecla)
            else:
                self.escolher_som(tecla)

        def menu(e):
            if tecla == ".":
                return
            m = tk.Menu(self.root, tearoff=0)
            m.add_command(label="Escolher som…", command=lambda: self.escolher_som(tecla))
            m.add_command(label="Gravar pelo microfone…", command=lambda: self.abrir_gravador(tecla))
            if self._caminho_slot(tecla):
                m.add_command(label="Tocar", command=lambda: self.tocar_slot(tecla))
                m.add_separator()
                m.add_command(label="Remover", command=lambda: self.remover_som(tecla))
            m.tk_popup(e.x_root, e.y_root)

        for w in (f, lbl_tecla, lbl_nome):
            w.bind("<Button-1>", clique)
            w.bind("<Button-3>", menu)
            w.bind("<Enter>", lambda _e, t=tecla: self._cor_pad(t, PAD_HOVER))
            w.bind("<Leave>", lambda _e, t=tecla: self._atualizar_pad(t))
        self._atualizar_pad(tecla)

    def _cor_pad(self, tecla, cor):
        for w in self.pads[tecla]:
            w.configure(bg=cor)

    def _atualizar_pad(self, tecla):
        _f, lbl_tecla, lbl_nome = self.pads[tecla]
        if tecla == ".":
            self._cor_pad(tecla, PAD)
            lbl_nome.configure(text="parar sons", fg=TEXTO_FRACO)
            return
        caminho = self._caminho_slot(tecla)
        if caminho:
            self._cor_pad(tecla, PAD)
            nome = caminho.stem
            lbl_nome.configure(text=nome if len(nome) <= 34 else nome[:32] + "…", fg=TEXTO_FRACO)
            lbl_tecla.configure(fg=TEXTO)
        else:
            self._cor_pad(tecla, PAD_VAZIO)
            lbl_nome.configure(text="vazio — clique para escolher", fg="#5a5a68")
            lbl_tecla.configure(fg="#5a5a68")

    def _piscar(self, tecla):
        if tecla not in self.pads:
            return
        self._cor_pad(tecla, DESTAQUE)
        self.pads[tecla][1].configure(fg=BG)
        self.root.after(160, lambda: self._atualizar_pad(tecla))

    def _atualizar_status(self):
        if self.ativo:
            self.lbl_status.configure(text="● ATIVO", bg=VERDE, fg=BG)
        else:
            self.lbl_status.configure(text="● INATIVO", bg="#3a3a48", fg=TEXTO)
        if self.icone is not None:
            self.icone.icon = self._imagem_icone(self.ativo)
            self.icone.title = f"VoicePad — {'ATIVO' if self.ativo else 'INATIVO'}"
            self.icone.update_menu()

    # ------------------------------------------------------------ bandeja
    @staticmethod
    def _imagem_icone(ativo):
        """Logo do VoicePad: colorida com bolinha verde (ativo) ou apagada em cinza (inativo)."""
        tam = 64
        try:
            img = Image.open(LOGO_PNG).convert("RGBA").resize((tam, tam), Image.LANCZOS)
        except OSError:
            img = Image.new("RGBA", (tam, tam), (124, 58, 237, 255))
        if ativo:
            d = ImageDraw.Draw(img)
            d.ellipse((38, 38, 63, 63), fill=BG)
            d.ellipse((42, 42, 59, 59), fill=VERDE)
        else:
            alfa = img.getchannel("A")
            img = ImageEnhance.Brightness(img.convert("L").convert("RGB")).enhance(0.75).convert("RGBA")
            img.putalpha(alfa)
        return img

    def _iniciar_bandeja(self):
        # Os callbacks rodam na thread do pystray: só colocam pedidos na fila.
        menu = pystray.Menu(
            pystray.MenuItem("Abrir", lambda: self.fila.put(("mostrar", None)), default=True),
            pystray.MenuItem("Modo VoicePad (Ctrl+0)", lambda: self.fila.put(("alternar", None)),
                             checked=lambda _item: self.ativo),
            pystray.MenuItem("Parar sons", lambda: self.fila.put(("parar", None))),
            pystray.MenuItem("Iniciar com o Windows", lambda: self.fila.put(("iniciar", None)),
                             checked=lambda _item: inicia_com_windows()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Sair", lambda: self.fila.put(("sair", None))),
        )
        self.icone = pystray.Icon("voicepad", self._imagem_icone(False),
                                  "VoicePad — INATIVO", menu)
        self.icone.run_detached()

    def _esconder(self):
        self.root.withdraw()
        if not self.avisou_bandeja:
            self.avisou_bandeja = True
            self._toast("Continuo rodando aqui na bandeja 👇", "#3a3a48")

    def _mostrar(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _toast(self, texto, cor, duracao=1400):
        """Aviso pequeno no canto da tela, visível mesmo com a janela minimizada."""
        if self.toast_win is not None:
            self.toast_win.destroy()
        if self.toast_job:
            self.root.after_cancel(self.toast_job)
        t = tk.Toplevel(self.root)
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        t.configure(bg=cor)
        tk.Label(t, text=texto, bg=cor, fg="white", font=("Segoe UI", 12, "bold"),
                 padx=18, pady=10).pack()
        t.update_idletasks()
        x = t.winfo_screenwidth() - t.winfo_width() - 24
        y = t.winfo_screenheight() - t.winfo_height() - 72
        t.geometry(f"+{x}+{y}")
        self.toast_win = t

        def fechar():
            if self.toast_win is t:
                t.destroy()
                self.toast_win = None
        self.toast_job = self.root.after(duracao, fechar)

    def _trocar_dispositivo(self, _e=None):
        nome = self.var_dispositivo.get()
        pedido = "" if nome == "Padrão do Windows" else nome
        self.audio.trocar_dispositivo(pedido)
        if not self.audio.abriu(pedido):
            self._toast(f"⚠  Não consegui abrir \"{nome}\".\nUsando a padrão do Windows.", "#b9770e", 4000)
        self._salvar_config()

    def _trocar_dispositivo2(self, _e=None):
        nome = self.var_dispositivo2.get()
        if not self.extra.definir(None if nome == NENHUMA else nome):
            self._toast(f"⚠  \"{nome}\" não está conectado.\nConecte e ela volta a tocar sozinha.", "#b9770e", 4000)
        self._salvar_config()

    def _sair(self):
        try:
            keyboard.unhook_all()
            if self.icone is not None:
                self.icone.stop()
            self.extra.fechar()
        finally:
            pygame.mixer.quit()
            self.root.destroy()


def ja_esta_aberto():
    """Impede duas cópias rodando (cada uma alternaria o Ctrl+0, anulando a outra)."""
    global _MUTEX
    _MUTEX = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\VoicePad")
    return ctypes.windll.kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS


def main():
    mp.freeze_support()
    # Faz a barra de tarefas mostrar a logo do VoicePad em vez da do Python.
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("VoicePad.App")
    if ja_esta_aberto():
        r = tk.Tk()
        r.withdraw()
        messagebox.showinfo(NOME_APP, "O VoicePad já está aberto.\nProcure o ícone na bandeja, perto do relógio.")
        return
    root = tk.Tk()
    try:
        app = App(root)
    except Exception as err:  # mostra o erro em vez de fechar em silêncio
        messagebox.showerror(NOME_APP, f"Erro ao iniciar:\n{err}")
        raise
    if "--bandeja" in sys.argv:
        root.withdraw()
        app.avisou_bandeja = True
    root.mainloop()


if __name__ == "__main__":
    main()
