import sqlite3
import json
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime
from config import DATABASE_FILE

logger = logging.getLogger(__name__)

class Database:
    def __init__(self, db_file: str = DATABASE_FILE):
        self.db_file = db_file
        self.init_database()
    
    def init_database(self):
        """Инициализирует базу данных и создает необходимые таблицы"""
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            
            # Таблица для хранения информации о пирах
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS peers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    peer_name TEXT UNIQUE NOT NULL,
                    peer_id TEXT UNIQUE NOT NULL,
                    job_id TEXT UNIQUE NOT NULL,
                    telegram_user_id INTEGER,
                    telegram_username TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expire_date TIMESTAMP,
                    is_active BOOLEAN DEFAULT 1,
                    payment_status TEXT DEFAULT 'unpaid',
                    stars_paid INTEGER DEFAULT 0,
                    last_payment_date TIMESTAMP,
                    notification_sent BOOLEAN DEFAULT 0,
                    expired_notification_sent BOOLEAN DEFAULT 0
                )
            ''')
            
            # Таблица для хранения логов операций
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS operation_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    peer_name TEXT,
                    operation TEXT,
                    details TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Таблица для хранения платежей
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payment_id TEXT UNIQUE NOT NULL,
                    user_id INTEGER NOT NULL,
                    amount INTEGER NOT NULL,
                    currency TEXT DEFAULT 'RUB',
                    status TEXT DEFAULT 'pending',
                    payment_method TEXT,
                    tariff_key TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    metadata TEXT
                )
            ''')
            
            # Миграция: добавляем новые колонки если их нет
            self._migrate_database(cursor)
            
            conn.commit()
            logger.info("База данных инициализирована")
    
    def _migrate_database(self, cursor):
        """Выполняет миграцию базы данных для добавления новых колонок"""
        try:
            # Проверяем существование колонки payment_status
            cursor.execute("PRAGMA table_info(peers)")
            columns = [column[1] for column in cursor.fetchall()]
            
            if 'payment_status' not in columns:
                cursor.execute("ALTER TABLE peers ADD COLUMN payment_status TEXT DEFAULT 'unpaid'")
                logger.info("Добавлена колонка payment_status")
            
            if 'stars_paid' not in columns:
                cursor.execute("ALTER TABLE peers ADD COLUMN stars_paid INTEGER DEFAULT 0")
                logger.info("Добавлена колонка stars_paid")
            
            if 'last_payment_date' not in columns:
                cursor.execute("ALTER TABLE peers ADD COLUMN last_payment_date TIMESTAMP")
                logger.info("Добавлена колонка last_payment_date")
            
            if 'notification_sent' not in columns:
                cursor.execute("ALTER TABLE peers ADD COLUMN notification_sent BOOLEAN DEFAULT 0")
                logger.info("Добавлена колонка notification_sent")
            
            if 'expired_notification_sent' not in columns:
                cursor.execute("ALTER TABLE peers ADD COLUMN expired_notification_sent BOOLEAN DEFAULT 0")
                logger.info("Добавлена колонка expired_notification_sent")

            # Добавляем колонки для новой системы тарифов
            if 'tariff_key' not in columns:
                cursor.execute("ALTER TABLE peers ADD COLUMN tariff_key TEXT")
                logger.info("Добавлена колонка tariff_key")
            
            if 'payment_method' not in columns:
                cursor.execute("ALTER TABLE peers ADD COLUMN payment_method TEXT")
                logger.info("Добавлена колонка payment_method")
            
            if 'rub_paid' not in columns:
                cursor.execute("ALTER TABLE peers ADD COLUMN rub_paid INTEGER DEFAULT 0")
                logger.info("Добавлена колонка rub_paid")
                
        except Exception as e:
            logger.error(f"Ошибка при миграции базы данных: {e}")
    
    def add_peer(self, peer_name: str, peer_id: str, job_id: str, 
                 telegram_user_id: int, telegram_username: str, expire_date: str, 
                 payment_status: str = 'paid', stars_paid: int = 0, 
                 tariff_key: str = None, payment_method: str = None, rub_paid: int = 0) -> bool:
        """
        Добавляет нового пира в базу данных
        
        Args:
            peer_name: Имя пира
            peer_id: ID пира в WireGuard
            job_id: ID job для ограничения
            telegram_user_id: ID пользователя Telegram
            telegram_username: Username пользователя Telegram
            expire_date: Дата истечения
            payment_status: Статус оплаты ('paid', 'unpaid')
            stars_paid: Количество оплаченных звезд
            tariff_key: Ключ тарифа (7_days, 30_days)
            payment_method: Способ оплаты (stars, yookassa)
            rub_paid: Количество оплаченных рублей
            
        Returns:
            True если успешно добавлен
        """
        try:
            with sqlite3.connect(self.db_file) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO peers (peer_name, peer_id, job_id, telegram_user_id, 
                                     telegram_username, created_at, expire_date, is_active, 
                                     payment_status, stars_paid, last_payment_date, 
                                     notification_sent, tariff_key, payment_method, rub_paid)
                    VALUES (?, ?, ?, ?, ?, datetime('now'), ?, 1, ?, ?, NULL, 0, ?, ?, ?)
                ''', (peer_name, peer_id, job_id, telegram_user_id, telegram_username, 
                      expire_date, payment_status, stars_paid, tariff_key, payment_method, rub_paid))
                conn.commit()
                
                # Логируем операцию
                self.log_operation(peer_name, "CREATE_PEER", f"Создан пир {peer_name}, тариф {tariff_key}")
                return True
                
        except sqlite3.IntegrityError as e:
            logger.error(f"Ошибка при добавлении пира {peer_name}: {e}")
            return False
    
    def get_peer_by_name(self, peer_name: str) -> Optional[Dict[str, Any]]:
        """Получает информацию о пире по имени"""
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM peers WHERE peer_name = ?', (peer_name,))
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def get_peer_by_telegram_id(self, telegram_user_id: int) -> Optional[Dict[str, Any]]:
        """Получает информацию о пире по Telegram ID пользователя"""
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM peers WHERE telegram_user_id = ? AND is_active = 1', (telegram_user_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def get_all_peers(self) -> List[Dict[str, Any]]:
        """Получает список всех активных пиров"""
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM peers WHERE is_active = 1 ORDER BY created_at DESC')
            return [dict(row) for row in cursor.fetchall()]
    
    def delete_peer(self, peer_name: str) -> bool:
        """
        Удаляет пира из базы данных (помечает как неактивного)
        
        Args:
            peer_name: Имя пира для удаления
            
        Returns:
            True если успешно удален
        """
        try:
            with sqlite3.connect(self.db_file) as conn:
                cursor = conn.cursor()
                cursor.execute('UPDATE peers SET is_active = 0 WHERE peer_name = ?', (peer_name,))
                conn.commit()
                
                # Логируем операцию
                self.log_operation(peer_name, "DELETE_PEER", f"Удален пир {peer_name}")
                return cursor.rowcount > 0
                
        except Exception as e:
            logger.error(f"Ошибка при удалении пира {peer_name}: {e}")
            return False
    
    def get_job_data(self, peer_name: str) -> Optional[Dict[str, Any]]:
        """Получает данные job для удаления"""
        peer = self.get_peer_by_name(peer_name)
        if not peer:
            return None
        
        return {
            "JobID": peer['job_id'],
            "Configuration": "awg0",  # Можно вынести в конфиг
            "Peer": peer['peer_id'],
            "Field": "date",
            "Operator": "lgt",
            "Value": peer['expire_date'],
            "CreationDate": peer['created_at'],
            "ExpireDate": peer['expire_date'],
            "Action": "restrict"
        }
    
    def log_operation(self, peer_name: str, operation: str, details: str):
        """Логирует операцию в базу данных"""
        try:
            with sqlite3.connect(self.db_file) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO operation_logs (peer_name, operation, details)
                    VALUES (?, ?, ?)
                ''', (peer_name, operation, details))
                conn.commit()
        except Exception as e:
            logger.error(f"Ошибка при логировании операции: {e}")
    
    def get_operation_logs(self, peer_name: str = None, limit: int = 50) -> List[Dict[str, Any]]:
        """Получает логи операций"""
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            if peer_name:
                cursor.execute('''
                    SELECT * FROM operation_logs 
                    WHERE peer_name = ? 
                    ORDER BY timestamp DESC 
                    LIMIT ?
                ''', (peer_name, limit))
            else:
                cursor.execute('''
                    SELECT * FROM operation_logs 
                    ORDER BY timestamp DESC 
                    LIMIT ?
                ''', (limit,))
            
            return [dict(row) for row in cursor.fetchall()]
    
    def get_expired_peers(self) -> List[Dict[str, Any]]:
        """Получает список пиров с истекшим сроком действия, ещё не уведомлённых"""
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM peers 
                WHERE is_active = 1 AND expire_date < datetime('now') AND expired_notification_sent = 0
                ORDER BY expire_date ASC
            ''')
            return [dict(row) for row in cursor.fetchall()]
    
    def update_peer_info(self, peer_name: str, new_peer_id: str, new_job_id: str, new_expire_date: str = None) -> bool:
        """
        Обновляет информацию о пире (ID, job_id и дату истечения)
        
        Args:
            peer_name: Имя пира
            new_peer_id: Новый ID пира
            new_job_id: Новый ID job
            new_expire_date: Новая дата истечения (опционально)
            
        Returns:
            True если успешно обновлен
        """
        try:
            with sqlite3.connect(self.db_file) as conn:
                cursor = conn.cursor()
                
                if new_expire_date:
                    cursor.execute('''
                        UPDATE peers 
                        SET peer_id = ?, job_id = ?, expire_date = ?
                        WHERE peer_name = ? AND is_active = 1
                    ''', (new_peer_id, new_job_id, new_expire_date, peer_name))
                else:
                    cursor.execute('''
                        UPDATE peers 
                        SET peer_id = ?, job_id = ?
                        WHERE peer_name = ? AND is_active = 1
                    ''', (new_peer_id, new_job_id, peer_name))
                
                conn.commit()
                
                # Логируем операцию
                self.log_operation(peer_name, "UPDATE_PEER", f"Обновлен пир {peer_name} с новым ID")
                return cursor.rowcount > 0
                
        except Exception as e:
            logger.error(f"Ошибка при обновлении пира {peer_name}: {e}")
            return False
    
    def update_payment_status(self, telegram_user_id: int, payment_status: str, amount_paid: int = 0, 
                             payment_method: str = None, tariff_key: str = None) -> bool:
        """
        Обновляет статус оплаты для пользователя
        
        Args:
            telegram_user_id: ID пользователя Telegram
            payment_status: Статус оплаты ('paid', 'unpaid')
            amount_paid: Количество оплаченных средств (звезды или рубли)
            payment_method: Способ оплаты ('stars', 'yookassa')
            tariff_key: Ключ тарифа (7_days, 30_days)
            
        Returns:
            True если успешно обновлен
        """
        try:
            with sqlite3.connect(self.db_file) as conn:
                cursor = conn.cursor()
                
                # Обновляем соответствующие поля в зависимости от способа оплаты
                if payment_method == 'stars':
                    cursor.execute('''
                        UPDATE peers 
                        SET payment_status = ?, stars_paid = ?, last_payment_date = datetime('now'),
                            payment_method = ?, tariff_key = ?
                        WHERE telegram_user_id = ? AND is_active = 1
                    ''', (payment_status, amount_paid, payment_method, tariff_key, telegram_user_id))
                elif payment_method == 'yookassa':
                    cursor.execute('''
                        UPDATE peers 
                        SET payment_status = ?, rub_paid = ?, last_payment_date = datetime('now'),
                            payment_method = ?, tariff_key = ?
                        WHERE telegram_user_id = ? AND is_active = 1
                    ''', (payment_status, amount_paid, payment_method, tariff_key, telegram_user_id))
                else:
                    # Обратная совместимость
                    cursor.execute('''
                        UPDATE peers 
                        SET payment_status = ?, stars_paid = ?, last_payment_date = datetime('now')
                        WHERE telegram_user_id = ? AND is_active = 1
                    ''', (payment_status, amount_paid, telegram_user_id))
                
                conn.commit()
                
                # Логируем операцию
                self.log_operation(f"user_{telegram_user_id}", "PAYMENT_UPDATE", 
                                 f"Обновлен статус оплаты: {payment_status}, {payment_method}: {amount_paid}, тариф: {tariff_key}")
                return cursor.rowcount > 0
                
        except Exception as e:
            logger.error(f"Ошибка при обновлении статуса оплаты для пользователя {telegram_user_id}: {e}")
            return False
    
    def extend_access(self, telegram_user_id: int, days: int = 30) -> tuple[bool, str]:
        """
        Продлевает доступ пользователя на указанное количество дней
        
        Args:
            telegram_user_id: ID пользователя Telegram
            days: Количество дней для продления
            
        Returns:
            Tuple (success: bool, new_expire_date: str)
        """
        try:
            with sqlite3.connect(self.db_file) as conn:
                cursor = conn.cursor()
                
                # Получаем текущую дату истечения
                cursor.execute('''
                    SELECT expire_date FROM peers 
                    WHERE telegram_user_id = ? AND is_active = 1
                ''', (telegram_user_id,))
                result = cursor.fetchone()
                
                if not result:
                    return False, ""
                
                current_expire_date = result[0]
                
                # Рассчитываем новую дату истечения
                cursor.execute('''
                    SELECT datetime(?, '+{} days')
                '''.format(days), (current_expire_date,))
                new_expire_date = cursor.fetchone()[0]
                
                # Обновляем дату истечения
                cursor.execute('''
                    UPDATE peers 
                    SET expire_date = ?, notification_sent = 0, expired_notification_sent = 0
                    WHERE telegram_user_id = ? AND is_active = 1
                ''', (new_expire_date, telegram_user_id))
                conn.commit()
                
                # Логируем операцию
                self.log_operation(f"user_{telegram_user_id}", "EXTEND_ACCESS", 
                                 f"Продлен доступ на {days} дней. Новая дата: {new_expire_date}")
                return cursor.rowcount > 0, new_expire_date
                
        except Exception as e:
            logger.error(f"Ошибка при продлении доступа для пользователя {telegram_user_id}: {e}")
            return False, ""
    
    def get_users_for_notification(self, days_before: int = 3) -> List[Dict[str, Any]]:
        """
        Получает пользователей, которым нужно отправить уведомление о скором истечении доступа
        
        Args:
            days_before: За сколько дней до истечения отправлять уведомление
            
        Returns:
            Список пользователей для уведомления
        """
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM peers 
                WHERE is_active = 1 
                AND payment_status = 'paid'
                AND notification_sent = 0
                AND expire_date <= datetime('now', '+{} days')
                AND expire_date > datetime('now')
                ORDER BY expire_date ASC
            '''.format(days_before))
            return [dict(row) for row in cursor.fetchall()]
    
    def mark_notification_sent(self, telegram_user_id: int) -> bool:
        """
        Отмечает, что уведомление отправлено пользователю
        
        Args:
            telegram_user_id: ID пользователя Telegram
            
        Returns:
            True если успешно отмечено
        """
        try:
            with sqlite3.connect(self.db_file) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE peers 
                    SET notification_sent = 1
                    WHERE telegram_user_id = ? AND is_active = 1
                ''', (telegram_user_id,))
                conn.commit()
                return cursor.rowcount > 0
                
        except Exception as e:
            logger.error(f"Ошибка при отметке уведомления для пользователя {telegram_user_id}: {e}")
            return False

    def mark_expired_notification_sent(self, telegram_user_id: int) -> bool:
        """
        Отмечает, что уведомление об истечении доступа отправлено пользователю (одноразово)
        """
        try:
            with sqlite3.connect(self.db_file) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE peers 
                    SET expired_notification_sent = 1
                    WHERE telegram_user_id = ? AND is_active = 1
                ''', (telegram_user_id,))
                conn.commit()
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Ошибка при отметке истёкшего уведомления для пользователя {telegram_user_id}: {e}")
            return False
    
    def add_payment(self, payment_id: str, user_id: int, amount: int, 
                   payment_method: str, tariff_key: str, metadata: dict = None) -> bool:
        """
        Добавляет новый платеж в базу данных
        
        Args:
            payment_id: ID платежа от ЮKassa
            user_id: ID пользователя Telegram
            amount: Сумма в копейках
            payment_method: Способ оплаты
            tariff_key: Ключ тарифа
            metadata: Дополнительные данные
            
        Returns:
            True если успешно добавлен
        """
        try:
            with sqlite3.connect(self.db_file) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO payments (payment_id, user_id, amount, payment_method, 
                                         tariff_key, metadata)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (payment_id, user_id, amount, payment_method, tariff_key, 
                      json.dumps(metadata) if metadata else None))
                conn.commit()
                
                # Логируем операцию
                self.log_operation(f"user_{user_id}", "CREATE_PAYMENT", 
                                 f"Создан платеж {payment_id}, сумма: {amount}")
                return True
                
        except sqlite3.IntegrityError as e:
            logger.error(f"Ошибка при добавлении платежа {payment_id}: {e}")
            return False
    
    def update_payment_status_by_id(self, payment_id: str, status: str) -> bool:
        """
        Обновляет статус платежа по ID
        
        Args:
            payment_id: ID платежа
            status: Новый статус (pending, succeeded, canceled, refunded)
            
        Returns:
            True если успешно обновлен
        """
        try:
            # Валидируем статус
            valid_statuses = ['pending', 'succeeded', 'canceled', 'refunded']
            if status not in valid_statuses:
                logger.error(f"Неверный статус платежа: {status}")
                return False
                
            with sqlite3.connect(self.db_file) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE payments 
                    SET status = ?, updated_at = datetime('now')
                    WHERE payment_id = ?
                ''', (status, payment_id))
                conn.commit()
                
                # Логируем операцию
                self.log_operation(f"payment_{payment_id}", "UPDATE_PAYMENT_STATUS", 
                                 f"Обновлен статус платежа на: {status}")
                return cursor.rowcount > 0
                
        except Exception as e:
            logger.error(f"Ошибка при обновлении статуса платежа {payment_id}: {e}")
            return False
    
    def get_payment_by_id(self, payment_id: str) -> Optional[Dict[str, Any]]:
        """
        Получает информацию о платеже по ID
        
        Args:
            payment_id: ID платежа
            
        Returns:
            Данные платежа или None
        """
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM payments WHERE payment_id = ?', (payment_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def get_payments_by_user(self, user_id: int) -> List[Dict[str, Any]]:
        """
        Получает все платежи пользователя
        
        Args:
            user_id: ID пользователя
            
        Returns:
            Список платежей
        """
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM payments 
                WHERE user_id = ? 
                ORDER BY created_at DESC
            ''', (user_id,))
            return [dict(row) for row in cursor.fetchall()]
    
    def get_pending_payments(self) -> List[Dict[str, Any]]:
        """
        Получает все ожидающие платежи
        
        Returns:
            Список ожидающих платежей
        """
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM payments 
                WHERE status = 'pending' 
                ORDER BY created_at ASC
            ''')
            return [dict(row) for row in cursor.fetchall()]
