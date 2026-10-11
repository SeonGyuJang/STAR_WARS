"""Builds a small two-figure test PDF (vector chart + raster image) for the tests."""
import sys

import pymupdf as fitz


def make(path):
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    body = ("We study a simple method for testing figure extraction. " * 6 + "\n") * 4
    page.insert_textbox(fitz.Rect(60, 50, 552, 200), body, fontsize=9)
    # Figure 1: vector bar chart
    shape = page.new_shape()
    shape.draw_rect(fitz.Rect(120, 220, 490, 420))
    for i, h in enumerate([60, 120, 90, 170]):
        shape.draw_rect(fitz.Rect(150 + i * 80, 410 - h, 200 + i * 80, 410))
    shape.finish(color=(0, 0, 0), fill=(0.2, 0.6, 0.8))
    shape.commit()
    page.insert_textbox(fitz.Rect(60, 430, 552, 460), "Figure 1: Accuracy of four methods on the benchmark.", fontsize=9)
    page.insert_textbox(fitz.Rect(60, 470, 552, 740), body, fontsize=9)
    page2 = doc.new_page(width=612, height=792)
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 200, 120), 0)
    pix.set_rect(pix.irect, (240, 120, 60))
    page2.insert_image(fitz.Rect(150, 80, 460, 270), pixmap=pix)
    page2.insert_textbox(fitz.Rect(60, 280, 552, 310), "Figure 2: Overview of the proposed pipeline.", fontsize=9)
    page2.insert_textbox(fitz.Rect(60, 320, 552, 600), body, fontsize=9)
    doc.save(path)


if __name__ == "__main__":
    make(sys.argv[1])
