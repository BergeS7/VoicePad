"""Gera a logo do VoicePad (logo.png e voicepad.ico) nesta pasta."""

from pathlib import Path

from PIL import Image, ImageDraw

AQUI = Path(__file__).resolve().parent
ROXO = (124, 58, 237)
ROSA = (236, 72, 153)
BRANCO = (255, 255, 255, 255)


def desenhar(tam=512):
    s = 4  # desenha 4x maior e reduz: bordas suaves
    T = tam * s
    # fundo em degradê diagonal roxo -> rosa
    grad = Image.new("RGBA", (T, T))
    px = grad.load()
    for y in range(T):
        for x in range(T):
            t = (x + y) / (2 * T)
            px[x, y] = tuple(int(ROXO[i] + (ROSA[i] - ROXO[i]) * t) for i in range(3)) + (255,)
    mascara = Image.new("L", (T, T), 0)
    ImageDraw.Draw(mascara).rounded_rectangle((0, 0, T - 1, T - 1), radius=int(T * 0.22), fill=255)
    img = Image.new("RGBA", (T, T), (0, 0, 0, 0))
    img.paste(grad, (0, 0), mascara)

    d = ImageDraw.Draw(img)
    u = T / 100  # unidade: 1% do tamanho
    cx = T / 2
    w = 5.2 * u  # espessura das linhas

    # cápsula do microfone
    d.rounded_rectangle((cx - 11 * u, 18 * u, cx + 11 * u, 58 * u), radius=11 * u, fill=BRANCO)
    # suporte em "U"
    d.arc((cx - 19 * u, 30 * u, cx + 19 * u, 68 * u), 0, 180, fill=BRANCO, width=int(w))
    # haste e base
    d.line((cx, 68 * u, cx, 80 * u), fill=BRANCO, width=int(w))
    d.rounded_rectangle((cx - 12 * u, 78 * u, cx + 12 * u, 78 * u + w), radius=w / 2, fill=BRANCO)
    # ondas de som dos dois lados
    for r, alpha in ((29, 255), (39, 170)):
        cor = (255, 255, 255, alpha)
        caixa = (cx - r * u, 38 * u - r * u, cx + r * u, 38 * u + r * u)
        d.arc(caixa, -35, 35, fill=cor, width=int(w * 0.8))
        d.arc(caixa, 145, 215, fill=cor, width=int(w * 0.8))

    return img.resize((tam, tam), Image.LANCZOS)


if __name__ == "__main__":
    logo = desenhar(512)
    logo.save(AQUI / "logo.png")
    logo.save(AQUI / "voicepad.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("ok:", AQUI / "logo.png", AQUI / "voicepad.ico")
