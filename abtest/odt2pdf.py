"""Refresh every field and index in .odt files, save them, and export each to PDF.

    /usr/bin/python3 odt2pdf.py PROFILE_DIR FILE.odt [FILE.odt ...]

A headless `soffice --convert-to pdf` leaves the table of contents and the
figure and table lists as they were saved, up to 6 pages off. This drives
LibreOffice over UNO instead: refresh, update the indexes twice (the second
pass picks up page numbers after the TOC itself settles), store, then export
FILE.pdf next to FILE.odt.

Needs the system Python that has the `uno` module (python3-uno), not a venv.
PROFILE_DIR is a scratch LibreOffice profile, so a running office is not
disturbed.
"""
import os
import subprocess
import sys
import time

import uno
from com.sun.star.beans import PropertyValue

PORT = 2083


def prop(name, value):
    p = PropertyValue()
    p.Name = name
    p.Value = value
    return p


def main(profile, files):
    proc = subprocess.Popen(['soffice', '-env:UserInstallation=file://' + os.path.abspath(profile),
                             '--headless', '--invisible',
                             '--accept=socket,host=127.0.0.1,port={};urp;'.format(PORT)])
    ctx = uno.getComponentContext()
    resolver = ctx.ServiceManager.createInstanceWithContext('com.sun.star.bridge.UnoUrlResolver', ctx)
    remote = None
    for _ in range(60):
        try:
            remote = resolver.resolve(
                'uno:socket,host=127.0.0.1,port={};urp;StarOffice.ComponentContext'.format(PORT))
            break
        except Exception:
            time.sleep(1)
    if remote is None:
        proc.kill()
        sys.exit('odt2pdf: LibreOffice did not start')
    desk = remote.ServiceManager.createInstanceWithContext('com.sun.star.frame.Desktop', remote)
    try:
        for src in files:
            doc = desk.loadComponentFromURL(uno.systemPathToFileUrl(os.path.abspath(src)),
                                            '_blank', 0, (prop('Hidden', True),))
            for _ in range(2):
                doc.refresh()
                doc.getTextFields().refresh()
                indexes = doc.getDocumentIndexes()
                for i in range(indexes.getCount()):
                    indexes.getByIndex(i).update()
            doc.store()
            out = os.path.splitext(os.path.abspath(src))[0] + '.pdf'
            doc.storeToURL(uno.systemPathToFileUrl(out), (prop('FilterName', 'writer_pdf_Export'),))
            doc.close(True)
            print('wrote', out)
    finally:
        try:
            desk.terminate()
        except Exception:
            pass
        proc.wait(timeout=30)


if __name__ == '__main__':
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2:])
