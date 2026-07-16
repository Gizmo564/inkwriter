Drop full-panel background images (PNG or JPG, any resolution) in this
folder to use them as the shutdown screen. Set:

    [display]
    shutdown_screen = custom

in ~/.config/inkwriter/config.ini. Images are cover-fit to the panel and
dithered to 1-bit automatically at draw time -- no pre-processing needed.
If multiple images are here, one is picked at random each shutdown. If
this folder is empty, Inkwriter falls back to the growth-stage art.
