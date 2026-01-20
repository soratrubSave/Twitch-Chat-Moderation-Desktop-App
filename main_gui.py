import sys
import os
import time
import socket
import logging
from collections import deque
from PyQt5.QtWidgets import (
    QApplication, QWidget, QPushButton, QLabel, QTextEdit, QVBoxLayout, QHBoxLayout,
    QFileDialog, QGroupBox, QGridLayout, QMessageBox, QFrame, QDialog, QListWidget, QLineEdit, QSystemTrayIcon, QStyle, QCheckBox, QTabWidget
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QThread, QObject, QMutex
from PyQt5.QtGui import QFont
import winsound
from datetime import datetime
import re
import pandas as pd

try:
    from wordsegment import load, segment # type: ignore
    WORDSEGMENT_AVAILABLE = True
    print("wordsegment imported successfully")
except ImportError:
    WORDSEGMENT_AVAILABLE = False
    print("wordsegment not available, falling back to basic detection")    

# Configure logging at the beginning of the script
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

class TwitchChatWorker(QObject):
    """Worker class สำหรับจัดการ Twitch chat connection"""
    message_received = pyqtSignal(str, str)  # username, message
    bad_word_detected = pyqtSignal(str, str, list)  # username, message, bad_words
    connection_status = pyqtSignal(bool, str)  # connected, status_message
    chat_stats = pyqtSignal(int, int)  # total_messages, bad_word_count
    error_occurred = pyqtSignal(str)  # error message
    
    def __init__(self, channel_name):
        super().__init__()
        self.logger = logging.getLogger(__name__) 
        self.channel_name = channel_name.lower()
        self.running = False
        self.socket = None
        self.badwords_th, self.badwords_en = self.load_bad_words()
        self.total_messages = 0
        self.bad_word_count = 0
        
        self.chat_messages = deque(maxlen=200) # Added maxlen to prevent infinite growth
        
        # เพิ่ม Thread Safety
        self.chat_mutex = QMutex()
        
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 5
    
        # โหลด wordsegment dictionary
        if WORDSEGMENT_AVAILABLE:
            try:
                load()  # โหลด dictionary ครั้งแรก
                print("wordsegment dictionary loaded successfully")
            except Exception as e:
                print(f"Error loading wordsegment dictionary: {e}")
        
    def load_bad_words(self):
        # โหลดแยก
        badwords_th = set()
        badwords_en = set()
        
        # โหลดคำไทย
        try:
            with open('badwords.txt', 'r', encoding='utf-8') as f:
                for line in f:
                    if line.strip():
                        badwords_th.add(line.strip().lower())
        except FileNotFoundError:
            self.logger.warning("Thai badwords.txt not found.")
        
        # โหลดคำอังกฤษ
        try:
            with open('badwords_en.txt', 'r', encoding='utf-8') as f:
                for line in f:
                    if line.strip():
                        badwords_en.add(line.strip().lower())
        except FileNotFoundError:
            self.logger.warning("English badwords_en.txt not found.")
        
        return badwords_th, badwords_en
    
    def detect_english_profanity(self, message):
        """ตรวจจับคำหยาบภาษาอังกฤษด้วย wordsegment + badwords_en - ปรับปรุงแล้ว"""
        try:
            self.logger.debug(f"🔍 Debug: detect_english_profanity called with: '{message}'")
            
            # ขั้นตอนที่ 1: Clean message
            cleaned_message = re.sub(r'[^a-zA-Z\s]', '', message.lower())
            self.logger.debug(f"🧹 Cleaned message: '{cleaned_message}'")
            
            # ขั้นตอนที่ 2: วิธีใหม่ - ใช้ wordsegment กับข้อความทั้งหมดเลย
            if WORDSEGMENT_AVAILABLE:
                try:
                    # ลบ space ออกแล้ว segment ทั้งหมด
                    message_no_space = cleaned_message.replace(' ', '')
                    segmented_words = segment(message_no_space)
                    self.logger.debug(f"✂️ Segmented entire message: '{message_no_space}' -> {segmented_words}")
                    
                    # รวมกับคำที่แยกด้วย space ด้วย (เผื่อคำปกติ)
                    normal_words = cleaned_message.split()
                    words_to_check = list(set(segmented_words + normal_words))  # ลบคำซ้ำ
                    self.logger.debug(f"🎯 Final words to check: {words_to_check}")
                    
                except Exception as e:
                    self.logger.warning(f"⚠️ wordsegment error: {e}")
                    words_to_check = cleaned_message.split()
            else:
                self.logger.debug("❌ wordsegment not available")
                words_to_check = cleaned_message.split()
            
            # ขั้นตอนที่ 3: ตรวจสอบแต่ละคำ
            found_words = []
            for word in words_to_check:
                if len(word) >= 3: 
                    if word in self.badwords_en:
                        found_words.append(word)
                        self.logger.info(f"🚨 Found English profanity: '{word}'")
            
            self.logger.debug(f"✅ Final result: {found_words}")
            return found_words
            
        except Exception as e:
            self.logger.error(f"❌ Error in detect_english_profanity: {e}")
            return []
                  
    def detect_thai_profanity(self, message):
        """ตรวจจับคำหยาบภาษาไทย"""
        try:
            found_words = set()
            message_lower = message.lower()
            message_clean = re.sub(r'[^a-zA-Zก-๙\s]', '', message_lower)
            message_clean_no_space = message_clean.replace(' ', '')
            
            # ตรวจสอบคำหยาบไทยกับข้อความที่ลบสัญลักษณ์และ space แล้ว
            for badword in self.badwords_th:
                if badword in message_clean_no_space:
                    found_words.add(badword)
            
            return list(found_words)
            
        except Exception as e:
            self.logger.error(f"Error in Thai profanity detection: {e}")
            return []
        
    def optimized_detect_bad_words(self, message):
        """ตรวจจับคำหยาบ"""
        try:
            all_found_words = []
            
            # ตรวจจับคำหยาบไทย
            thai_words = self.detect_thai_profanity(message)
            all_found_words.extend(thai_words)
            
            # ตรวจจับคำหยาบอังกฤษ
            english_words = self.detect_english_profanity(message)
            all_found_words.extend(english_words)
            
            # ลบคำซ้ำ
            return list(set(all_found_words))
            
        except Exception as e:
            self.logger.error(f"Error in optimized bad word detection: {e}")
            self.error_occurred.emit(f"Detection error: {e}")
            return []
           
    def connect_to_twitch(self):
        """เชื่อมต่อกับ Twitch IRC"""
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.settimeout(10)  # ตั้งค่า timeout
            self.socket.connect(('irc.chat.twitch.tv', 6667))
            
            # ส่งข้อมูล authentication
            # ใช้ anonymous user (justinfan + random number)
            self.socket.send(f'NICK justinfan{int(time.time())}\n'.encode('utf-8'))
            self.socket.send(f'JOIN #{self.channel_name}\n'.encode('utf-8'))
            
            # รอการตอบสนอง
            time.sleep(1)
            
            self.connection_status.emit(True, f"เชื่อมต่อกับ {self.channel_name} สำเร็จ")
            self.reconnect_attempts = 0
            self.logger.info(f"Successfully connected to {self.channel_name}")
            return True
            
        except socket.timeout:
            error_msg = "Connection timeout"
            self.logger.error(error_msg)
            self.connection_status.emit(False, error_msg)
            self.error_occurred.emit(error_msg)
            return False
        except socket.gaierror:
            error_msg = "DNS resolution failed"
            self.logger.error(error_msg)
            self.connection_status.emit(False, error_msg)
            self.error_occurred.emit(error_msg)
            return False
        except ConnectionRefusedError:
            error_msg = "Connection refused by server"
            self.logger.error(error_msg)
            self.connection_status.emit(False, error_msg)
            self.error_occurred.emit(error_msg)
            return False
        except Exception as e:
            error_msg = f"Connection error: {e}"
            self.logger.error(error_msg)
            self.connection_status.emit(False, error_msg)
            self.error_occurred.emit(error_msg)
            # Cleanup ในกรณีที่เกิด error
            if self.socket:
                try:
                    self.socket.close()
                    self.socket = None
                except:
                    pass
            return False
    
    def listen_to_chat(self):
        """ฟังข้อความจาก chat"""
        buffer = ""  # เพิ่ม buffer สำหรับข้อมูลที่ไม่สมบูรณ์
        
        while self.running:
            try:
                # ตรวจสอบว่า socket ยังใช้งานได้หรือไม่
                if not self.socket:
                    self.logger.error("Socket is None, cannot receive data")
                    break
                
                self.socket.settimeout(0.5) # Set a small timeout for graceful shutdown
                data = self.socket.recv(1024).decode('utf-8', errors='ignore')  # เพิ่ม error handling สำหรับ encoding
                if not data:
                    self.logger.debug("No data received, continuing loop.")
                    continue
                
                # เพิ่มข้อมูลใหม่เข้า buffer
                buffer += data
                
                # แยกข้อความที่สมบูรณ์ (ลงท้ายด้วย \n)
                while '\n' in buffer:
                    line, buffer = buffer.split('\n', 1)
                    line = line.rstrip('\r')
                    
                    if not line:
                        continue
                    
                    # จัดการ PING/PONG
                    if line.startswith('PING'):
                        if self.socket:  # ตรวจสอบอีกครั้งก่อนส่ง
                            pong_response = 'PONG :tmi.twitch.tv\r\n'
                            self.socket.send(pong_response.encode('utf-8'))
                        continue
                    
                    # แยกข้อความ chat
                    if 'PRIVMSG' in line:
                        self.process_chat_message(line)
                    
            except socket.timeout:
                continue # This is expected behavior, just continue the loop
            except socket.error as e:
                if self.running:
                    self.logger.error(f"Socket error: {e}")
                    self.handle_connection_error(f"Socket error: {e}")
                    continue
            except UnicodeDecodeError as e:
                self.logger.warning(f"Unicode decode error: {e}")
                continue
            except Exception as e:
                if self.running:
                    error_msg = f"Chat listening error: {e}"
                    self.logger.error(error_msg)
                    self.error_occurred.emit(error_msg)
                    self.handle_connection_error(error_msg)
                    break

    def handle_connection_error(self, error_message):
        """จัดการ connection error แบบรวมศูนย์"""
        self.connection_status.emit(False, f"การเชื่อมต่อขาด: {error_message}")
        
        # ลองเชื่อมต่อใหม่
        while self.reconnect_attempts < self.max_reconnect_attempts and self.running:
            self.reconnect_attempts += 1
            retry_msg = f"กำลังลองเชื่อมต่อใหม่... ({self.reconnect_attempts}/{self.max_reconnect_attempts})"
            self.logger.info(retry_msg)
            self.connection_status.emit(False, retry_msg)
            
            # รอก่อนลองใหม่ (exponential backoff)
            wait_time = min(5 * (2 ** (self.reconnect_attempts - 1)), 30)  # 5, 10, 20, 30 วินาที
            time.sleep(wait_time)
            
            if self.connect_to_twitch():
                return True
                
        self.logger.error("Max reconnection attempts reached")
        return False
        
    def process_chat_message(self, line):
        """แยกการประมวลผลข้อความ chat เป็นฟังก์ชันแยก"""
        try:
            # ตัวอย่าง: :username!username@username.tmi.twitch.tv PRIVMSG #channel :message
            parts = line.split('PRIVMSG')
            if len(parts) >= 2:
                user_part = parts[0].split('!')[0][1:]  # เอา username
                message_part = parts[1].split(':', 1)
                if len(message_part) < 2:
                    return
                    
                message = message_part[1].strip()
                
                self.total_messages += 1
                
                # ส่งข้อความทั่วไป
                self.message_received.emit(user_part, message)
                
                # ตรวจจับคำหยาบ
                found_words = self.optimized_detect_bad_words(message)
                
                if found_words:
                    self.bad_word_count += 1
                    
                    chat_info = {
                        'timestamp': datetime.now(),
                        'username': user_part,
                        'message': message,
                        'bad_words': found_words,
                        'channel': self.channel_name
                    }
                    
                    # ใช้ mutex เพื่อ thread safety
                    self.chat_mutex.lock()
                    try:
                        self.chat_messages.append(chat_info)
                    finally:
                        self.chat_mutex.unlock()
                    
                    # ส่งสัญญาณพบคำหยาบ
                    self.bad_word_detected.emit(user_part, message, found_words)
                    
                    # อัพเดทสถิติ
                    self.chat_stats.emit(self.total_messages, self.bad_word_count)
                    
        except Exception as e:
            self.logger.error(f"Error processing chat message: {e}")            
    
    def start_listening(self):
        """เริ่มการฟัง chat"""
        if self.connect_to_twitch():
            self.running = True
            self.listen_to_chat()
    
    def stop_listening(self):
        """หยุดการฟัง chat"""
        self.running = False
        if self.socket:
            try:
                self.socket.close()
                self.socket = None  # ตั้งค่าเป็น None หลังจากปิด
                self.logger.info("Socket closed successfully")
            except Exception as e:
                self.logger.error(f"Error closing socket: {e}")
                self.socket = None  # ตั้งค่าเป็น None แม้จะ error
    
    def get_chat_messages(self):
        """ดึงข้อความแชทแบบ thread-safe"""
        self.chat_mutex.lock()
        try:
            return list(self.chat_messages)
        finally:
            self.chat_mutex.unlock()
    
    def clear_memory_messages(self):
        """ล้างข้อความในหน่วยความจำของ worker"""
        self.chat_mutex.lock()
        try:
            self.chat_messages.clear()
            self.total_messages = 0
            self.bad_word_count = 0
        finally:
            self.chat_mutex.unlock()

class TwitchChatThread(QThread):
    """Thread สำหรับจัดการ Twitch chat"""
    
    def __init__(self, channel_name):
        super().__init__()
        self.worker = TwitchChatWorker(channel_name)
        self.worker.moveToThread(self)
        
        # เชื่อมต่อ thread signals
        self.started.connect(self.worker.start_listening)
        self.finished.connect(self.worker.stop_listening)
    
    def run(self):
        """เริ่มการทำงานของ thread"""
        # ใช้ event loop ภายใน QThread เพื่อให้ signal/slot ทำงานถูกต้อง
        self.exec_()

class BadWordManagerDialog(QDialog):
    def __init__(self, badwords_file, parent=None):
        super().__init__(parent)
        self.setWindowTitle('จัดการคำหยาบ')
        self.badwords_file = badwords_file
        self.setMinimumWidth(400)
        self.list_widget = QListWidget()
        self.input_line = QLineEdit()
        self.input_line.setPlaceholderText('เพิ่มคำหยาบใหม่...')
        
        # เพิ่มช่องค้นหา
        self.search_line = QLineEdit()
        self.search_line.setPlaceholderText('ค้นหาคำหยาบ...')
        self.search_line.textChanged.connect(self.filter_words)
        
        add_btn = QPushButton('เพิ่ม')
        del_btn = QPushButton('ลบที่เลือก')
        save_btn = QPushButton('บันทึก')
        close_btn = QPushButton('ปิด')
        add_btn.clicked.connect(self.add_word)
        del_btn.clicked.connect(self.delete_selected)
        save_btn.clicked.connect(self.save_words)
        close_btn.clicked.connect(self.close)
        btn_layout = QHBoxLayout()
        btn_layout.addWidget(add_btn)
        btn_layout.addWidget(del_btn)
        btn_layout.addWidget(save_btn)
        btn_layout.addWidget(close_btn)
        layout = QVBoxLayout()
        layout.addWidget(QLabel('คำหยาบทั้งหมด:'))
        layout.addWidget(self.search_line)
        layout.addWidget(self.list_widget)
        layout.addWidget(self.input_line)
        layout.addLayout(btn_layout)
        self.setLayout(layout)
        self.all_words = []  # เก็บคำทั้งหมดไว้สำหรับ filter
        self.load_words()

    def load_words(self):
        self.list_widget.clear()
        self.all_words = []
        try:
            with open(self.badwords_file, 'r', encoding='utf-8') as f:
                for line in f:
                    word = line.strip()
                    if word:
                        self.all_words.append(word)
                        self.list_widget.addItem(word)
        except FileNotFoundError:
            pass

    def filter_words(self):
        search = self.search_line.text().strip().lower()
        self.list_widget.clear()
        for word in self.all_words:
            if search in word.lower():
                self.list_widget.addItem(word)

    def add_word(self):
        word = self.input_line.text().strip()
        if word and not self.list_widget.findItems(word, Qt.MatchExactly):
            self.all_words.append(word)
            self.filter_words()
            self.input_line.clear()

    def delete_selected(self):
        for item in self.list_widget.selectedItems():
            word = item.text()
            self.all_words = [w for w in self.all_words if w != word]
            self.list_widget.takeItem(self.list_widget.row(item))
        self.filter_words()

    def save_words(self):
        try:
            with open(self.badwords_file, 'w', encoding='utf-8') as f:
                for word in self.all_words:
                    f.write(word + '\n')
            QMessageBox.information(self, 'บันทึกสำเร็จ', 'บันทึกคำหยาบเรียบร้อยแล้ว')
        except Exception as e:
            QMessageBox.warning(self, 'ผิดพลาด', f'ไม่สามารถบันทึกได้: {e}')

class DashboardWindow(QWidget):
    def __init__(self, parent=None):
        super().__init__()
        self.parent = parent
        self.setWindowTitle('Dashboard สถิติ - Bad Word Detector')
        self.setGeometry(300, 300, 700, 500)
        self.setFont(QFont('Tahoma', 10))
        self.init_ui()
        
        # Timer สำหรับอัพเดทข้อมูล
        self.update_timer = QTimer()
        self.update_timer.timeout.connect(self.update_stats)
        self.update_timer.start(1000)  # อัพเดททุก 1 วินาที

    def init_ui(self):
        layout = QVBoxLayout()
        layout.setSpacing(20)
        layout.setContentsMargins(30, 30, 30, 30)

        # หัวข้อ
        title_label = QLabel('📊 Dashboard สถิติการตรวจจับ')
        title_label.setStyleSheet('font-size: 20px; font-weight: bold; color: #2196F3; text-align: center;')
        layout.addWidget(title_label)

        # สถิติการตรวจจับ
        stats_group = QGroupBox('📈 สถิติการทำงาน')
        stats_layout = QGridLayout()
        
        # จำนวนคำหยาบที่พบวันนี้
        detection_count_label = QLabel('จำนวนคำหยาบที่พบวันนี้:')
        detection_count_label.setStyleSheet('font-weight: bold; color: #d32f2f; font-size: 14px;')
        self.detection_count_label = QLabel('0')
        self.detection_count_label.setStyleSheet('font-size: 24px; font-weight: bold; color: #d32f2f;')
        stats_layout.addWidget(detection_count_label, 0, 0)
        stats_layout.addWidget(self.detection_count_label, 0, 1)
        
        # จำนวนข้อความทั้งหมด
        total_messages_label = QLabel('จำนวนข้อความทั้งหมด:')
        total_messages_label.setStyleSheet('font-weight: bold; color: #2196F3; font-size: 14px;')
        self.total_messages_label = QLabel('0')
        self.total_messages_label.setStyleSheet('font-size: 24px; font-weight: bold; color: #2196F3;')
        stats_layout.addWidget(total_messages_label, 1, 0)
        stats_layout.addWidget(self.total_messages_label, 1, 1)
        
        # เวลาการทำงาน
        working_time_label = QLabel('เวลาการทำงาน:')
        working_time_label.setStyleSheet('font-weight: bold; color: #4CAF50; font-size: 14px;')
        self.working_time_label = QLabel('00:00:00')
        self.working_time_label.setStyleSheet('font-size: 24px; font-weight: bold; color: #4CAF50;')
        stats_layout.addWidget(working_time_label, 2, 0)
        stats_layout.addWidget(self.working_time_label, 2, 1)
        
        # ความถี่การตรวจจับ
        detection_freq_label = QLabel('ความถี่การตรวจจับ:')
        detection_freq_label.setStyleSheet('font-weight: bold; color: #FF9800; font-size: 14px;')
        self.detection_freq_label = QLabel('0 ครั้ง/นาที')
        self.detection_freq_label.setStyleSheet('font-size: 24px; font-weight: bold; color: #FF9800;')
        stats_layout.addWidget(detection_freq_label, 3, 0)
        stats_layout.addWidget(self.detection_freq_label, 3, 1)
        
        # อัตราส่วนคำหยาบ
        ratio_label = QLabel('อัตราส่วนคำหยาบ:')
        ratio_label.setStyleSheet('font-weight: bold; color: #9C27B0; font-size: 14px;')
        self.ratio_label = QLabel('0%')
        self.ratio_label.setStyleSheet('font-size: 24px; font-weight: bold; color: #9C27B0;')
        stats_layout.addWidget(ratio_label, 4, 0)
        stats_layout.addWidget(self.ratio_label, 4, 1)
        
        stats_group.setLayout(stats_layout)
        layout.addWidget(stats_group)

        # เพิ่ม Performance Statistics
        perf_group = QGroupBox('⚡ สถิติประสิทธิภาพ')
        perf_layout = QGridLayout()
        
        # เวลาการตรวจจับเฉลี่ย
        avg_detection_label = QLabel('เวลาตรวจจับเฉลี่ย:')
        avg_detection_label.setStyleSheet('font-weight: bold; color: #607D8B; font-size: 14px;')
        self.avg_detection_label = QLabel('0.000 วินาที')
        self.avg_detection_label.setStyleSheet('font-size: 18px; font-weight: bold; color: #607D8B;')
        perf_layout.addWidget(avg_detection_label, 0, 0)
        perf_layout.addWidget(self.avg_detection_label, 0, 1)
        
        # การใช้ Memory
        memory_usage_label = QLabel('การใช้ Memory:')
        memory_usage_label.setStyleSheet('font-weight: bold; color: #795548; font-size: 14px;')
        self.memory_usage_label = QLabel('0 KB')
        self.memory_usage_label.setStyleSheet('font-size: 18px; font-weight: bold; color: #795548;')
        perf_layout.addWidget(memory_usage_label, 1, 0)
        perf_layout.addWidget(self.memory_usage_label, 1, 1)
        
        # จำนวน Error
        error_count_label = QLabel('จำนวน Error:')
        error_count_label.setStyleSheet('font-weight: bold; color: #F44336; font-size: 14px;')
        self.error_count_label = QLabel('0')
        self.error_count_label.setStyleSheet('font-size: 18px; font-weight: bold; color: #F44336;')
        perf_layout.addWidget(error_count_label, 2, 0)
        perf_layout.addWidget(self.error_count_label, 2, 1)
        
        # สถานะการเชื่อมต่อ
        connection_status_label = QLabel('สถานะการเชื่อมต่อ:')
        connection_status_label.setStyleSheet('font-weight: bold; color: #009688; font-size: 14px;')
        self.connection_status_label = QLabel('ไม่เชื่อมต่อ')
        self.connection_status_label.setStyleSheet('font-size: 18px; font-weight: bold; color: #009688;')
        perf_layout.addWidget(connection_status_label, 3, 0)
        perf_layout.addWidget(self.connection_status_label, 3, 1)
        
        perf_group.setLayout(perf_layout)
        layout.addWidget(perf_group)

        # ปุ่มควบคุม
        control_layout = QHBoxLayout()
        
        # ปุ่มล้างสถิติ
        clear_stats_btn = QPushButton('🗑️ ล้างสถิติ')
        clear_stats_btn.clicked.connect(self.clear_stats)
        clear_stats_btn.setStyleSheet('background-color: #FF5722; font-size: 12px; padding: 8px;')
        
       
        
        # ปุ่มปิด
        close_btn = QPushButton('ปิด Dashboard')
        close_btn.clicked.connect(self.close)
        close_btn.setStyleSheet('background-color: #FF5722; font-size: 14px; padding: 10px;')
        
        control_layout.addWidget(clear_stats_btn)
        control_layout.addWidget(close_btn)
        layout.addLayout(control_layout)

        self.setLayout(layout)

    def update_stats(self):
        """อัพเดทสถิติจากหน้าหลัก"""
        if self.parent:
            try:
                # อัพเดทจำนวนคำหยาบที่พบวันนี้
                self.detection_count_label.setText(str(self.parent.detection_count))
                
                # อัพเดทจำนวนข้อความทั้งหมด
                self.total_messages_label.setText(str(self.parent.twitch_total_messages))
                
                # อัพเดทเวลาการทำงาน
                if self.parent.start_time:
                    elapsed = datetime.now() - self.parent.start_time
                    hours = elapsed.seconds // 3600
                    minutes = (elapsed.seconds % 3600) // 60
                    seconds = elapsed.seconds % 60
                    self.working_time_label.setText(f'{hours:02d}:{minutes:02d}:{seconds:02d}')
                
                # อัพเดทความถี่การตรวจจับ (คำนวณจาก 1 นาทีล่าสุด)
                now = datetime.now()
                recent_detections = [t for t in self.parent.detection_times if (now - t).seconds <= 60]
                freq = len(recent_detections)
                self.detection_freq_label.setText(f'{freq} ครั้ง/นาที')
                 
                # อัพเดทอัตราส่วนคำหยาบ
                if self.parent.twitch_total_messages > 0:
                    ratio = (self.parent.detection_count / self.parent.twitch_total_messages) * 100
                    self.ratio_label.setText(f'{ratio:.2f}%')
                else:
                    self.ratio_label.setText('0%')
                
                # อัพเดทสถิติประสิทธิภาพ
                if hasattr(self.parent, 'performance_stats'):
                    # เวลาการตรวจจับเฉลี่ย
                    avg_time_ms = self.parent.performance_stats.get('avg_detection_time', 0) * 1000
                    self.avg_detection_label.setText(f'{avg_time_ms:.2f} ms')
                    
                    # การใช้ Memory
                    memory_usage = self.parent.performance_stats.get('memory_usage', 0)
                    self.memory_usage_label.setText(f'{memory_usage:.1f} KB')
                    
                    # จำนวน Error
                    error_count = getattr(self.parent, 'error_count', 0)
                    self.error_count_label.setText(str(error_count))
                    
                    # สถานะการเชื่อมต่อ
                    if self.parent.twitch_thread and self.parent.twitch_thread.isRunning():
                        self.connection_status_label.setText('เชื่อมต่อแล้ว')
                        self.connection_status_label.setStyleSheet('font-size: 18px; font-weight: bold; color: #4CAF50;')
                    else:
                        self.connection_status_label.setText('ไม่เชื่อมต่อ')
                        self.connection_status_label.setStyleSheet('font-size: 18px; font-weight: bold; color: #F44336;')
                        
            except Exception as e:
                print(f"Error updating dashboard stats: {e}")

    def clear_stats(self):
        """ล้างสถิติทั้งหมด"""
        try:
            if self.parent:
                # ใช้เมทอดหลักเพื่อให้ UI ใต้ Twitch channel ถูกรีเซ็ตด้วย
                self.parent.clear_chat_messages()
                self.parent.detection_count = 0
                self.parent.detection_times = []
                self.parent.twitch_total_messages = 0
                self.parent.twitch_bad_word_count = 0
                self.parent.error_count = 0
                
                # รีเซ็ต performance stats
                if hasattr(self.parent, 'performance_stats'):
                    self.parent.performance_stats = {
                        'avg_detection_time': 0,
                        'total_detection_time': 0,
                        'detection_count': 0,
                        'memory_usage': 0
                    }
                
                QMessageBox.information(self, 'ล้างสถิติ', 'ล้างสถิติทั้งหมดเรียบร้อยแล้ว')
                
        except Exception as e:
            QMessageBox.warning(self, 'ข้อผิดพลาด', f'ไม่สามารถล้างสถิติได้: {e}')

    def apply_dark_mode(self):
        """ใช้ Dark Mode Style Sheet กับหน้าต่าง Dashboard"""
        self.setStyleSheet("""
            QWidget {
                background-color: #2d2d2d;
                color: #ffffff;
            }
            QGroupBox {
                border: 2px solid #4CAF50;
                border-radius: 6px;
                margin-top: 12px;
                font-weight: bold;
                padding-top: 10px;
                background-color: #3d3d3d;
            }
            QLabel {
                color: #ffffff;
            }
            QPushButton {
                background-color: #FF5722;
                color: white;
                border: none;
                padding: 8px 16px;
                border-radius: 4px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #E64A19;
            }
        """)
    
class BadWordDetectorApp(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Twitch Bad Word Detector - Enhanced Version')
        self.setGeometry(200, 200, 800, 700)
        self.setFont(QFont('Tahoma', 10))
        self.bad_words = self.load_all_bad_words()
        self.sound_file = None
        
        # เพิ่มตัวแปรสำหรับ Dashboard
        self.detection_count = 0
        self.start_time = None
        self.detection_times = []
        
        # เพิ่มตัวแปรสำหรับ Twitch mode
        self.twitch_thread = None
        self.twitch_total_messages = 0
        self.twitch_bad_word_count = 0
        
        # เพิ่ม Performance Monitoring
        self.performance_stats = {
            'avg_detection_time': 0,
            'total_detection_time': 0,
            'detection_count': 0,
            'memory_usage': 0
        }
        
        # เพิ่ม Error Tracking
        self.error_count = 0
        self.last_error_time = None
        
        self.tray_icon = QSystemTrayIcon(self)
        style = self.style()
        icon = style.standardIcon(QStyle.SP_MessageBoxWarning)
        self.tray_icon.setIcon(icon)
        self.tray_icon.setVisible(True)
        
        # เพิ่ม Performance Timer
        self.performance_timer = QTimer()
        self.performance_timer.timeout.connect(self.update_performance_stats)
        self.performance_timer.start(5000)  # อัพเดททุก 5 วินาที
        
        self.apply_default_style()
        self.init_ui()

    def update_performance_stats(self):
        """อัพเดทสถิติประสิทธิภาพ"""
        try:
            if self.twitch_thread and self.twitch_thread.worker:
                # คำนวณ memory usage 
                message_count = len(self.twitch_thread.worker.get_chat_messages())
                memory_usage = message_count * 0.1  # KB per message
                self.performance_stats['memory_usage'] = memory_usage
                
                # ปรับเกณฑ์ warning ให้เหมาะกับ 200 ข้อความ
                if memory_usage > 15:  # 15 KB (75% ของ 200 ข้อความ)
                    self.show_memory_warning(memory_usage)
                    
        except Exception as e:
            self.log_error(f"Error updating performance stats: {e}")

    def show_memory_warning(self, memory_usage):
        """แสดง warning เมื่อ memory ใช้เยอะ (เตือนแค่ครั้งเดียว)"""
        # เช็คว่าเคยเตือนไปหรือยัง? (ถ้ายังไม่เคย หรือค่าเป็น False ให้ทำต่อ)
        if not getattr(self, 'memory_warning_shown', False):
            
            # 1. ตั้งค่าว่า "เตือนแล้วนะ" (ครั้งหน้าจะได้ไม่เข้าเงื่อนไขนี้อีก)
            self.memory_warning_shown = True
            
            # 2. แสดงข้อความแจ้งเตือน
            msg = f'การใช้หน่วยความจำสูง: {memory_usage:.1f} KB\n'
            msg += f'ข้อความที่เก็บ: {int(memory_usage / 0.1)} ข้อความ\n'
            msg += 'ระบบจะไม่แจ้งเตือนซ้ำจนกว่าคุณจะกด "ล้างข้อมูลทั้งหมด"'
            
            QMessageBox.information(self, 'แจ้งเตือนการใช้หน่วยความจำ', msg)

    def show_user_friendly_error(self, error_type, error_message):
        """แสดง error message ที่เข้าใจง่ายสำหรับผู้ใช้"""
        error_titles = {
            'connection': 'การเชื่อมต่อล้มเหลว',
            'detection': 'ข้อผิดพลาดในการตรวจจับ',
            'memory': 'ข้อผิดพลาดหน่วยความจำ',
            'file': 'ข้อผิดพลาดไฟล์',
            'general': 'ข้อผิดพลาดทั่วไป'
        }
        
        title = error_titles.get(error_type, 'ข้อผิดพลาด')
        
        # แสดง notification
        self.tray_icon.showMessage(
            title,
            error_message,
            QSystemTrayIcon.Critical,
            5000
        )
        
        # แสดง dialog ถ้าเป็น error สำคัญ
        if error_type in ['connection', 'memory']:
            QMessageBox.critical(self, title, error_message)

    def apply_default_style(self):
        """ใช้ style เริ่มต้น (Light Mode)"""
        self.setStyleSheet("""
            QWidget {
                background-color: #f0f0f0;
            }
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                padding: 8px 16px;
                border-radius: 4px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #45a049;
            }
            QPushButton:disabled {
                background-color: #cccccc;
            }
            QGroupBox {
                border: 2px solid #4CAF50;
                border-radius: 6px;
                margin-top: 12px;
                font-weight: bold;
                padding-top: 10px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
            }
            QTextEdit {
                border: 1px solid #cccccc;
                border-radius: 4px;
                padding: 5px;
                background-color: white;
            }
            QLabel {
                color: #333333;
            }
            QLineEdit {
                padding: 5px;
                border: 1px solid #cccccc;
                border-radius: 4px;
                background-color: white;
            }
            QCheckBox {
                color: #333333;
                font-weight: bold;
            }
        """)

    def init_ui(self):
        layout = QVBoxLayout() # จัดเรียง widget แบบแนวตั้ง
        layout.setSpacing(15) # ระยะห่างระหว่าง widget
        layout.setContentsMargins(20, 20, 20, 20) # ขอบรอบๆ layout (ซ้าย, บน, ขวา, ล่าง)

        # Header
        header_label = QLabel('🎮 Twitch Bad Word Detector')
        header_label.setStyleSheet('font-size: 24px; font-weight: bold; color: #9146FF; text-align: center; margin: 10px;')
        layout.addWidget(header_label)

        # Status
        status_frame = QFrame() # กรอบสำหรับแสดงสถานะ
        status_frame.setFrameStyle(QFrame.StyledPanel) # สไตล์กรอบแบบ panel
        status_layout = QHBoxLayout() # layout แนวนอนสำหรับสถานะ
        self.status_label = QLabel('สถานะ: พร้อมใช้งาน') # ข้อความสถานะ (เก็บไว้ใน self เพื่ออัพเดตได้)
        self.status_label.setStyleSheet('color: #4CAF50; font-weight: bold; font-size: 12px;')
        status_layout.addWidget(self.status_label) # ใส่ label ลงไปใน layout
        status_frame.setLayout(status_layout)  # นำ layout มาใส่ในกรอบ
        layout.addWidget(status_frame) # เพิ่มกรอบนี้ลงใน layout หลัก

        # ข้อมูลระบบตรวจจับ
        detection_info = QLabel('🔍 ตรวจจับคำหยาบ: ไทย + อังกฤษ')
        detection_info.setStyleSheet('font-weight: bold; color: #2196F3; font-size: 12px;')
        layout.addWidget(detection_info)

        # Badword manager buttons
        # ปุ่มสำหรับเปิดหน้าต่างจัดการคำหยาบภาษาไทย
        badword_mgr_btn = QPushButton('จัดการคำหยาบ (ไทย)') 
        badword_mgr_btn.clicked.connect(lambda: self.open_badword_manager('badwords.txt'))

        # -> เมื่อกดปุ่ม จะเรียกฟังก์ชัน self.open_badword_manager() 
        #    โดยส่งชื่อไฟล์ badwords.txt เข้าไป (ไฟล์เก็บคำหยาบภาษาไทย)

        # ปุ่มสำหรับเปิดหน้าต่างจัดการคำหยาบภาษาอังกฤษ
        badword_mgr_en_btn = QPushButton('จัดการคำหยาบ (อังกฤษ)')
        badword_mgr_en_btn.clicked.connect(lambda: self.open_badword_manager('badwords_en.txt'))

        # จัด layout แบบแนวนอน สำหรับวางปุ่มทั้งสองข้าง ๆ กัน
        btn_mgr_layout = QHBoxLayout()
        btn_mgr_layout.addWidget(badword_mgr_btn)
        btn_mgr_layout.addWidget(badword_mgr_en_btn)

        # นำ layout ปุ่มจัดการ badword ใส่เข้าไปใน layout หลัก
        layout.addLayout(btn_mgr_layout)

        # Twitch settings
        # กล่อง (GroupBox) สำหรับรวมการตั้งค่า Twitch
        self.twitch_group = QGroupBox('การตั้งค่า Twitch')
        
        twitch_layout = QGridLayout() # ใช้ layout แบบตาราง (grid)
        
        # แถวที่ 0: ใส่ label "Channel Name:" และกล่อง input สำหรับกรอกชื่อแชนแนล
        twitch_layout.addWidget(QLabel('Channel Name:'), 0, 0)
        self.channel_input = QLineEdit()   # สร้างช่องกรอกข้อความ
        self.channel_input.setPlaceholderText('ใส่ชื่อ channel (เช่น: ninja)')  
        # -> placeholder = ตัวอย่างข้อความที่โผล่มาจาง ๆ
        twitch_layout.addWidget(self.channel_input, 0, 1)

        # แถวที่ 2: ข้อความทั้งหมดที่ตรวจจับได้
        twitch_layout.addWidget(QLabel('ข้อความทั้งหมด:'), 2, 0)
        self.twitch_total_label = QLabel('0')  
        # -> เริ่มต้นที่ 0 (ยังไม่พบข้อความ)
        self.twitch_total_label.setStyleSheet('font-weight: bold; color: #2196F3;')  
        # -> ตัวหนา, น้ำเงิน
        twitch_layout.addWidget(self.twitch_total_label, 2, 1)

        # แถวที่ 3: จำนวนคำหยาบที่พบ
        twitch_layout.addWidget(QLabel('คำหยาบที่พบ:'), 3, 0)
        self.twitch_bad_label = QLabel('0')  
        # -> เริ่มต้นที่ 0 (ยังไม่เจอคำหยาบ)
        self.twitch_bad_label.setStyleSheet('font-weight: bold; color: #d32f2f;')  
        # -> ตัวหนา, สีแดง
        twitch_layout.addWidget(self.twitch_bad_label, 3, 1)

        # ใส่ layout ตารางลงในกล่อง groupbox
        self.twitch_group.setLayout(twitch_layout)
        # เพิ่ม groupbox นี้เข้าไปใน layout หลัก
        layout.addWidget(self.twitch_group)

        # -------------------- Control Buttons --------------------
        # กล่องรวมปุ่มควบคุมการตรวจจับ
        control_group = QGroupBox('ควบคุมการตรวจจับ')
        control_layout = QHBoxLayout()

        # ปุ่มสำหรับเชื่อมต่อ Twitch
        self.twitch_connect_btn = QPushButton('เชื่อมต่อ Twitch')
        # ปุ่มสำหรับยกเลิกการเชื่อมต่อ
        self.twitch_disconnect_btn = QPushButton('ยกเลิกการเชื่อมต่อ')

        # กำหนดการทำงานเมื่อกดปุ่ม
        self.twitch_connect_btn.clicked.connect(self.connect_twitch)  # เชื่อมต่อ
        self.twitch_disconnect_btn.clicked.connect(self.disconnect_twitch)  # ยกเลิกการเชื่อมต่อ
        self.twitch_disconnect_btn.setEnabled(False)  
        # -> ตอนแรกปุ่ม disconnect ใช้งานไม่ได้ เพราะยังไม่ได้เชื่อมต่อ

        # วางปุ่มทั้งสองใน layout แนวนอน
        control_layout.addWidget(self.twitch_connect_btn)
        control_layout.addWidget(self.twitch_disconnect_btn)

        # ใส่ layout ลงใน groupbox แล้วเพิ่ม groupbox ลงใน layout หลัก
        control_group.setLayout(control_layout)
        layout.addWidget(control_group)

        # -------------------- Dashboard Button --------------------
        # ปุ่มสำหรับเปิดหน้า dashboard
        dashboard_btn = QPushButton('📊 เปิด Dashboard')
        dashboard_btn.clicked.connect(self.open_dashboard)  # กดแล้วเปิด dashboard
        dashboard_btn.setStyleSheet('background-color: #2196F3; font-size: 14px; padding: 10px;')  
        # -> ปรับสไตล์ให้ปุ่มดูเด่น
        layout.addWidget(dashboard_btn)

        # -------------------- Settings --------------------
        # กล่องการตั้งค่าทั่วไป
        settings_group = QGroupBox('การตั้งค่า')
        settings_layout = QGridLayout()
        settings_layout.setSpacing(15)  # ระยะห่างระหว่าง widgets

        # ---- ตั้งค่าเสียงเตือน ----
        sound_label = QLabel('เสียงเตือน:')
        sound_label.setStyleSheet('font-weight: bold;')
        settings_layout.addWidget(sound_label, 0, 0)

        # ปุ่มเลือกไฟล์เสียง
        self.sound_btn = QPushButton('เลือกไฟล์เสียง (.wav)')
        self.sound_btn.clicked.connect(self.select_sound)
        settings_layout.addWidget(self.sound_btn, 0, 1)

        settings_group.setLayout(settings_layout)
        layout.addWidget(settings_group)

        # -------------------- Results --------------------
        # กล่องสำหรับแสดงผลลัพธ์
        results_group = QGroupBox('ผลการตรวจจับ')
        results_layout = QVBoxLayout()

        # ใช้ Tab widget แยกผลลัพธ์ (เช่น คำหยาบ / แชท)
        self.results_tab = QTabWidget()

        # --- Tab 1: คำหยาบที่พบ ---
        badword_tab = QWidget()
        badword_layout = QVBoxLayout()

        badword_label = QLabel('คำหยาบที่พบ:')
        badword_label.setStyleSheet('font-weight: bold; color: #d32f2f;')  # สีแดง
        badword_layout.addWidget(badword_label)

        self.badword_text = QTextEdit()   # กล่องข้อความโชว์คำหยาบ
        self.badword_text.setMaximumHeight(100)
        self.badword_text.setPlaceholderText('คำหยาบที่พบจะแสดงที่นี่...')
        badword_layout.addWidget(self.badword_text)

        badword_tab.setLayout(badword_layout)
        self.results_tab.addTab(badword_tab, 'คำหยาบที่พบ')

        # --- Tab 2: ข้อความแชท ---
        chat_tab = QWidget()
        chat_layout = QVBoxLayout()

        chat_label = QLabel('ข้อความแชทล่าสุด:')
        chat_label.setStyleSheet('font-weight: bold; color: #2196F3;')  # สีน้ำเงิน
        chat_layout.addWidget(chat_label)

        self.chat_text = QTextEdit()  # กล่องข้อความโชว์แชท
        self.chat_text.setMaximumHeight(150)
        self.chat_text.setPlaceholderText('ข้อความแชทจะแสดงที่นี่ (Twitch mode)...')
        chat_layout.addWidget(self.chat_text)

        # ปุ่มล้างข้อความแชท
        clear_chat_btn = QPushButton('🗑️ ล้างข้อมูลทั้งหมด')
        clear_chat_btn.setToolTip('ล้างข้อความแชท, คำหยาบ, และข้อมูลในหน่วยความจำ')
        clear_chat_btn.clicked.connect(self.clear_chat_messages)
        clear_chat_btn.setStyleSheet('background-color: #FF5722; font-size: 12px;')
        chat_layout.addWidget(clear_chat_btn)

        chat_tab.setLayout(chat_layout)
        self.results_tab.addTab(chat_tab, 'ข้อความแชท')

        # เพิ่ม tab widget ลงใน layout ผลลัพธ์
        results_layout.addWidget(self.results_tab)
        results_group.setLayout(results_layout)
        layout.addWidget(results_group)

        # -------------------- Export Log Button --------------------
        # ปุ่ม export log เป็นไฟล์ CSV
        export_btn = QPushButton('Export Log (CSV)')
        export_btn.clicked.connect(self.export_log)
        layout.addWidget(export_btn)

        # -------------------- UI/UX ปรับปรุง --------------------
        # Dark Mode Toggle
        dark_mode_layout = QHBoxLayout()
        self.dark_mode_checkbox = QCheckBox('Dark Mode')
        self.dark_mode_checkbox.clicked.connect(self.toggle_dark_mode)
        dark_mode_layout.addWidget(self.dark_mode_checkbox)

        # แสดง shortcut keys ให้ผู้ใช้
        shortcuts_info = QLabel('⌨️ Shortcuts: Ctrl+D: Dark Mode | Ctrl+E: Export Log | Ctrl+R: Reset Stats')
        shortcuts_info.setStyleSheet(
            'font-size: 10px; color: #666; background-color: #f9f9f9; padding: 5px; border-radius: 3px;'
        )
        dark_mode_layout.addWidget(shortcuts_info)

        layout.addLayout(dark_mode_layout)

        # -------------------- Apply Layout --------------------
        self.setLayout(layout)   # ตั้งค่า layout หลักให้กับ UI นี้

    def connect_twitch(self):
        """เชื่อมต่อ Twitch"""
        try:
            channel = self.channel_input.text().strip()
            if not channel:
                QMessageBox.warning(self, 'ข้อผิดพลาด', 'กรุณาใส่ชื่อ channel')
                return
            
            # Validate channel name
            if not re.match(r'^[a-zA-Z0-9_]{4,25}$', channel):
                QMessageBox.warning(self, 'ข้อผิดพลาด', 'ชื่อ channel ไม่ถูกต้อง (4-25 ตัวอักษร, ตัวอักษรและตัวเลขเท่านั้น)')
                return
            
            self.twitch_thread = TwitchChatThread(channel) 
            
            # เชื่อมต่อ signals
            self.twitch_thread.worker.message_received.connect(self.on_twitch_message)
            self.twitch_thread.worker.bad_word_detected.connect(self.on_twitch_bad_word)
            self.twitch_thread.worker.connection_status.connect(self.on_twitch_connection_status)
            self.twitch_thread.worker.chat_stats.connect(self.on_twitch_stats)
            self.twitch_thread.worker.error_occurred.connect(self.on_twitch_error)
            
            self.twitch_thread.start()
            
            # อัพเดทสถานะปุ่ม
            self.twitch_connect_btn.setEnabled(False)
            self.twitch_disconnect_btn.setEnabled(True)
            
            self.status_label.setText('สถานะ: กำลังเชื่อมต่อ Twitch...')
            self.status_label.setStyleSheet('color: #FFA500; font-weight: bold; font-size: 12px;')
            
            # รีเซ็ตสถิติและเริ่มจับเวลา
            self.start_time = datetime.now()
            self.twitch_total_messages = 0
            self.twitch_bad_word_count = 0
            self.detection_count = 0
            self.detection_times = []
            self.twitch_total_label.setText('0')
            self.twitch_bad_label.setText('0')
            
        except Exception as e:
            error_msg = f"Error connecting to Twitch: {e}"
            self.log_error(error_msg)
            self.show_user_friendly_error('connection', error_msg)

    def disconnect_twitch(self):
        """ยกเลิกการเชื่อมต่อ Twitch"""
        try:
            if self.twitch_thread:
                # สั่งให้ worker หยุด loop และปิด socket อย่างปลอดภัยก่อน
                try:
                    self.twitch_thread.worker.stop_listening()
                except Exception:
                    pass
                # จากนั้นปิด event loop ของ QThread
                self.twitch_thread.quit()
                self.twitch_thread.wait(5000)  # รอสูงสุด 5 วินาที
                if self.twitch_thread.isRunning():
                    self.twitch_thread.terminate()  # Force terminate
                self.twitch_thread = None
            
            # อัพเดทสถานะปุ่ม
            self.twitch_connect_btn.setEnabled(True)
            self.twitch_disconnect_btn.setEnabled(False)
            
            self.status_label.setText('สถานะ: ยกเลิกการเชื่อมต่อ Twitch')
            self.status_label.setStyleSheet('color: #FFA500; font-weight: bold; font-size: 12px;')
            
        except Exception as e:
            error_msg = f"Error disconnecting from Twitch: {e}"
            self.log_error(error_msg)
            self.show_user_friendly_error('connection', error_msg)

    def on_twitch_message(self, username, message):
        """เมื่อได้รับข้อความจาก Twitch"""
        try:
            self.twitch_total_messages += 1
            self.twitch_total_label.setText(str(self.twitch_total_messages))
            
            # แสดงข้อความใน chat tab
            timestamp = datetime.now().strftime('%H:%M:%S')
            chat_line = f'[{timestamp}] {username}: {message}'
            
            # เพิ่มข้อความใหม่ที่ด้านบน
            current_text = self.chat_text.toPlainText()
            if current_text:
                chat_line = chat_line + '\n' + current_text
            
            # จำกัดจำนวนบรรทัด (เก็บ 50 บรรทัดล่าสุด)
            lines = chat_line.split('\n')
            if len(lines) > 50:
                lines = lines[:50]
                chat_line = '\n'.join(lines)
            
            self.chat_text.setPlainText(chat_line)
            
        except Exception as e:
            self.log_error(f"Error processing Twitch message: {e}")

    def on_twitch_bad_word(self, username, message, bad_words):
        """เมื่อพบคำหยาบใน Twitch"""
        try:
            start_time = time.perf_counter()
            
            timestamp = datetime.now().strftime('%H:%M:%S')
            
            # แสดงใน badword tab
            current_badword_text = self.badword_text.toPlainText()
            new_line = f'[{timestamp}] {username}: {message} -> พบคำหยาบ: {", ".join(bad_words)}'
            
            if current_badword_text:
                new_line = new_line + '\n' + current_badword_text
            
            # จำกัดจำนวนบรรทัด (เก็บ 20 บรรทัดล่าสุด)
            lines = new_line.split('\n')
            if len(lines) > 20:
                lines = lines[:20]
                new_line = '\n'.join(lines)
            
            self.badword_text.setPlainText(new_line)
            
            # อัพเดทสถิติ
            self.detection_count += 1
            self.detection_times.append(datetime.now())
            self.twitch_bad_word_count += 1
            self.twitch_bad_label.setText(str(self.twitch_bad_word_count))
            
            # คำนวณเวลาการตรวจจับ
            detection_time = time.perf_counter() - start_time
            self.performance_stats['total_detection_time'] += detection_time
            self.performance_stats['detection_count'] += 1
            self.performance_stats['avg_detection_time'] = (
                self.performance_stats['total_detection_time'] / 
                self.performance_stats['detection_count']
            )
            
            # เล่นเสียงเตือน
            self.play_alert()
            
            # แสดง notification
            self.tray_icon.showMessage(
                'พบคำหยาบใน Twitch',
                f'{username}: {", ".join(bad_words)}',
                QSystemTrayIcon.Warning,
                3000
            )
            
        except Exception as e:
            self.log_error(f"Error processing bad word detection: {e}")

    def on_twitch_connection_status(self, connected, message):
        """เมื่อสถานะการเชื่อมต่อ Twitch เปลี่ยน"""
        try:
            if connected:
                self.status_label.setText(f'สถานะ: {message}')
                self.status_label.setStyleSheet('color: #4CAF50; font-weight: bold; font-size: 12px;')
                # อัพเดทสถานะปุ่มเมื่อเชื่อมต่อสำเร็จ
                self.twitch_connect_btn.setEnabled(False)
                self.twitch_disconnect_btn.setEnabled(True)
                
                # แสดงข้อความใน chat tab
                timestamp = datetime.now().strftime('%H:%M:%S')
                self.chat_text.setPlainText(f'[{timestamp}] ✅ {message}\n')
            else:
                self.status_label.setText(f'สถานะ: {message}')
                self.status_label.setStyleSheet('color: #FF0000; font-weight: bold; font-size: 12px;')
                # อัพเดทสถานะปุ่มเมื่อการเชื่อมต่อขาด
                self.twitch_connect_btn.setEnabled(True)
                self.twitch_disconnect_btn.setEnabled(False)
                
                # แสดงข้อความใน chat tab
                timestamp = datetime.now().strftime('%H:%M:%S')
                current_text = self.chat_text.toPlainText()
                self.chat_text.setPlainText(f'[{timestamp}] ❌ {message}\n' + current_text)
                
        except Exception as e:
            self.log_error(f"Error handling connection status: {e}")

    def on_twitch_stats(self, total_messages, bad_word_count):
        """อัพเดทสถิติ Twitch"""
        try:
            self.twitch_total_messages = total_messages
            self.twitch_bad_word_count = bad_word_count
            
            # อัพเดท label ใน UI
            self.twitch_total_label.setText(str(total_messages))
            self.twitch_bad_label.setText(str(bad_word_count))
            
        except Exception as e:
            self.log_error(f"Error updating Twitch stats: {e}")

    def on_twitch_error(self, error_message):
        """เมื่อเกิดข้อผิดพลาดใน Twitch"""
        try:
            self.log_error(f"Twitch Error: {error_message}")
            self.status_label.setText(f'สถานะ: ข้อผิดพลาด Twitch - {error_message}')
            self.status_label.setStyleSheet('color: #FF0000; font-weight: bold; font-size: 12px;')
            
            # แสดง error dialog ถ้าเป็น error สำคัญ
            if 'connection' in error_message.lower() or 'socket' in error_message.lower():
                self.show_user_friendly_error('connection', error_message)
                
        except Exception as e:
            self.log_error(f"Error handling Twitch error: {e}")

    def clear_chat_messages(self):
        """ล้างข้อความแชท"""
        try:
            self.chat_text.clear()
            timestamp = datetime.now().strftime('%H:%M:%S')
            self.chat_text.setPlaceholderText('ข้อความแชทจะแสดงที่นี่ (Twitch mode)...')
            
            # ล้างข้อความใน worker ด้วย
            if self.twitch_thread and self.twitch_thread.worker:
                self.twitch_thread.worker.clear_memory_messages()
                
            # ล้างคำหยาบที่แสดงใน UI ด้วย
            self.badword_text.clear()
            self.badword_text.setPlaceholderText('คำหยาบที่พบจะแสดงที่นี่...')
            
            # รีเซ็ตสถิติการตรวจจับ
            self.detection_count = 0
            self.detection_times = []
            self.twitch_total_messages = 0
            self.twitch_bad_word_count = 0
            self.twitch_total_label.setText('0')
            self.twitch_bad_label.setText('0')
            self.memory_warning_shown = False
                
        except Exception as e:
            self.log_error(f"Error clearing chat messages: {e}")

    def select_sound(self):
        file, _ = QFileDialog.getOpenFileName(self, 'เลือกไฟล์เสียง', '', 'WAV Files (*.wav)')
        if file:
            self.sound_file = file
            self.sound_btn.setText(os.path.basename(file))
        else:
            self.sound_file = None
            self.sound_btn.setText('เลือกไฟล์เสียง (.wav)')

    def load_all_bad_words(self):
        badwords = []
        for filename in ['badwords.txt', 'badwords_en.txt']:
            try:
                with open(filename, 'r', encoding='utf-8') as f:
                    badwords += [line.strip() for line in f if line.strip()]
            except FileNotFoundError:
                continue
        return badwords

    def play_alert(self):
        if self.sound_file:
            try:
                winsound.PlaySound(self.sound_file, winsound.SND_FILENAME | winsound.SND_ASYNC)
            except Exception as e:
                print(f'ไม่สามารถเล่นเสียง: {e}')
        else:
            winsound.Beep(1000, 500)

    def open_badword_manager(self, filename):
        dlg = BadWordManagerDialog(filename, self)
        dlg.exec_()
        # reload badwords after editing
        self.bad_words = self.load_all_bad_words()
        # อัพเดท bad_words ใน worker ด้วย
        if self.twitch_thread and self.twitch_thread.worker:
            self.twitch_thread.worker.badwords_th, self.twitch_thread.worker.badwords_en = self.twitch_thread.worker.load_bad_words()

    def export_log(self):
        """Export log พร้อม error handling"""
        try:
            if not hasattr(self, 'twitch_thread') or not self.twitch_thread or not hasattr(self.twitch_thread.worker, 'get_chat_messages'):
                QMessageBox.information(self, 'Export Log', 'ยังไม่มีข้อมูลประวัติการตรวจจับจาก Twitch')
                return
            
            file, _ = QFileDialog.getSaveFileName(self, 'บันทึกไฟล์ CSV', '', 'CSV Files (*.csv)')
            if file:
                export_data = []
                
                # Export ข้อมูล Twitch mode
                for chat_msg in self.twitch_thread.worker.get_chat_messages():
                    export_data.append({
                        'วันที่': chat_msg['timestamp'].strftime('%Y-%m-%d %H:%M:%S'),
                        'Channel': chat_msg['channel'],
                        'Username': chat_msg['username'],
                        'ข้อความ': chat_msg['message'],
                        'คำหยาบที่พบ': ', '.join(chat_msg['bad_words']),
                        'จำนวนคำหยาบ': len(chat_msg['bad_words'])
                    })
                
                df = pd.DataFrame(export_data)
                df.to_csv(file, index=False, encoding='utf-8-sig')
                QMessageBox.information(self, 'Export Log', f'บันทึกไฟล์ CSV สำเร็จแล้ว\nพบข้อมูล {len(export_data)} รายการ')
                
        except PermissionError:
            error_msg = "ไม่มีสิทธิ์เขียนไฟล์ กรุณาเลือกตำแหน่งอื่น"
            self.show_user_friendly_error('file', error_msg)
        except Exception as e:
            error_msg = f"Error exporting log: {e}"
            self.log_error(error_msg)
            self.show_user_friendly_error('file', error_msg)
    
    def log_error(self, message):
        """Centralized error logging and counting"""
        logging.error(message)
        self.error_count += 1
        self.last_error_time = datetime.now()

    def closeEvent(self, event):
        """จัดการเมื่อปิดโปรแกรม"""
        try:
            # หยุด performance timer
            if hasattr(self, 'performance_timer'):
                self.performance_timer.stop()
            
            # ปิดการเชื่อมต่อ Twitch และรอ thread จบ
            if self.twitch_thread:
                # เก็บ reference เดิมไว้ก่อน เพราะ disconnect_twitch จะเซ็ตเป็น None
                thread = self.twitch_thread
                self.disconnect_twitch()
                # รอ thread จบก่อนปิดโปรแกรม
                if thread and thread.isRunning():
                    thread.wait(3000)  # รอ 3 วินาที
                    if thread.isRunning():
                        thread.terminate()  # Force terminate
                        thread.wait(1000)  # รออีก 1 วินาที
            
            
            event.accept()
            
        except Exception as e:
            self.log_error(f"Error during application shutdown: {e}")
            event.accept()

    def toggle_dark_mode(self):
        """สลับ Dark Mode"""
        if self.dark_mode_checkbox.isChecked():
            self.setStyleSheet("""
                QWidget {
                    background-color: #2d2d2d;
                    color: #ffffff;
                }
                QPushButton {
                    background-color: #4CAF50;
                    color: white;
                    border: none;
                    padding: 8px 16px;
                    border-radius: 4px;
                    font-weight: bold;
                }
                QPushButton:hover {
                    background-color: #45a049;
                }
                QPushButton:disabled {
                    background-color: #555555;
                    color: #888888;
                }
                QGroupBox {
                    border: 2px solid #4CAF50;
                    border-radius: 6px;
                    margin-top: 12px;
                    font-weight: bold;
                    padding-top: 10px;
                    background-color: #3d3d3d;
                }
                QTextEdit {
                    border: 1px solid #555555;
                    border-radius: 4px;
                    padding: 5px;
                    background-color: #3d3d3d;
                    color: #ffffff;
                }
                QLabel {
                    color: #ffffff;
                }
                QLineEdit {
                    padding: 5px;
                    border: 1px solid #555555;
                    border-radius: 4px;
                    background-color: #3d3d3d;
                    color: #ffffff;
                }
                QCheckBox {
                    color: #ffffff;
                    font-weight: bold;
                }
                QTabWidget::pane {
                    border: 1px solid #555555;
                    background-color: #3d3d3d;
                }
                QTabBar::tab {
                    background-color: #2d2d2d;
                    color: #ffffff;
                    padding: 8px 16px;
                    border: 1px solid #555555;
                }
                QTabBar::tab:selected {
                    background-color: #4CAF50;
                }
            """)
        else:
            self.apply_default_style()

    def reset_stats(self):
        """รีเซ็ตสถิติ"""
        # ใช้เมทอดเดียวกับปุ่มล้างข้อมูลเพื่อให้ UI และตัวนับทั้งหมดสอดคล้องกัน
        self.clear_chat_messages()
        self.start_time = datetime.now()
        QMessageBox.information(self, 'รีเซ็ตสถิติ', 'รีเซ็ตสถิติเรียบร้อยแล้ว')

    def keyPressEvent(self, event):
        """จัดการ Keyboard Shortcuts"""
        if event.key() == Qt.Key_D and event.modifiers() == Qt.ControlModifier:
            # Ctrl+D: Dark Mode
            self.dark_mode_checkbox.setChecked(not self.dark_mode_checkbox.isChecked())
            self.toggle_dark_mode()
        elif event.key() == Qt.Key_E and event.modifiers() == Qt.ControlModifier:
            # Ctrl+E: Export Log
            self.export_log()
        elif event.key() == Qt.Key_R and event.modifiers() == Qt.ControlModifier:
            # Ctrl+R: Reset Stats
            self.reset_stats()
        else:
            super().keyPressEvent(event)

    def open_dashboard(self):
        """เปิดหน้า Dashboard"""
    
        self.dashboard_window = DashboardWindow(self)
        
        # ตรวจสอบว่า Dark Mode กำลังทำงานหรือไม่
        if self.dark_mode_checkbox.isChecked():
            self.dashboard_window.apply_dark_mode() 
            
        self.dashboard_window.show()
       

if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = BadWordDetectorApp()
    window.show()
    sys.exit(app.exec_())