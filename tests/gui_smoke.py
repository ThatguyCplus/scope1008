import sys, time
import os; os.makedirs('shots', exist_ok=True)
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer, QSettings
from scope1008.app import MainWindow, dark_palette
app = QApplication(sys.argv); dark_palette(app)
QSettings("Scope1008","Scope1008").clear()
mode = sys.argv[1]
w = MainWindow(demo=(mode != 'nodevice')); w.resize(1600, 950); w.show()
def pump(sec):
    end=time.time()+sec
    while time.time()<end: app.processEvents(); time.sleep(0.01)
pump(1.5)
if mode == 'nodevice':
    pump(2); w.grab().save('shots/nodevice.png'); print(w.status_lbl.text(), '|', w.banner_lbl.text()[:200])
elif mode == 'burst':
    for i in [2,3]: w.cards[i].btn.setChecked(True)
    w.cards[1].vdiv.setCurrentIndex(w.cards[1].vdiv.findText('2 V/div')); w.cards[1].offset.setValue(1.5)
    w.cards[0].offset.setValue(-3.5)
    w.cards[2].offset.setValue(-1.5)
    w.cards[3].vdiv.setCurrentIndex(w.cards[3].vdiv.findText('5 V/div')); w.cards[3].offset.setValue(-3.9)
    w.cards[0].name.setText('PWM'); w.cards[0].name.editingFinished.emit()
    w.trig_level = 2.5; w._sync_all(); w.push_config()
    w.cur_btn.setChecked(True); w.fft_btn.setChecked(True)
    pump(3)
    w.grab().save('shots/burst.png'); print('info', w.info_lbl.text(), w.trig_state, [w.table.item(0,c).text() for c in range(10)])
    w.set_mode('normal'); w.cards[1].btn.setChecked(True); w.trig_src.setCurrentIndex(1); w.trig_level=0.5; w._sync_all(); w.push_config(); pump(2)
    print('normal crank', w.trig_state, [w.table.item(1,c).text() for c in range(10)] if w.table.rowCount()>1 else None)
    w.do_single(); pump(2); print('single ->', w.running, w.trig_state)
elif mode == 'roll':
    for i in [3,4,5]: w.cards[i].btn.setChecked(True)
    w.cards[0].btn.setChecked(False); w.cards[1].btn.setChecked(False)
    w.cards[4].vdiv.setCurrentIndex(w.cards[4].vdiv.findText('200 mV/div'))
    w.cards[3].offset.setValue(-3)
    w.cards[3].vdiv.setCurrentIndex(w.cards[3].vdiv.findText('5 V/div'))
    w.cards[5].offset.setValue(-3)
    w.set_mode('roll'); pump(5)
    w.grab().save('shots/roll.png'); print('info', w.info_lbl.text())
w.close(); pump(0.5)
