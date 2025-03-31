import hashlib
from datetime import datetime, timedelta
import binascii
import json
import sys
import os
import time
import urllib3
import urllib
from urllib.parse import quote
import logging
from http.cookies import SimpleCookie
import ssl
import socket
from cryptography import x509
from cryptography.hazmat.backends import default_backend
import re

# Disable warnings for insecure connections
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
log_certificate_details = False  # Set to True if you want to see certificate details in logs
# Configure the logger
logger = logging.getLogger('homeassistant.components.zte_router')

if __name__ == "__main__":
    # Configure logging when run directly
    logger.setLevel(logging.DEBUG)

    # Common formatter
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    # Console handler (keeps existing behavior)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # File handler for mc.log in same directory as script
    script_dir = os.path.dirname(os.path.abspath(__file__))
    log_path = os.path.join(script_dir, "mc.log")

    file_handler = logging.FileHandler(log_path, mode='a')  # Append mode
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

else:
    # Suppress logging when imported
    logger.setLevel(logging.WARNING)

gsm = ("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
       "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ`¿abcdefghijklmnopqrstuvwxyzäöñüà")
ext = ("``````````````````^```````````````````{}`````\\````````````[~]`"
       "|````````````````````````````````````€``````````````````````````")

# Create a PoolManager instance to handle HTTP requests
s = urllib3.PoolManager(cert_reqs='CERT_NONE')

def get_sms_time():
    logger.debug("Generating SMS time")
    return datetime.now().strftime("%y;%m;%d;%H;%M;%S;+2")

def gsm_encode(plaintext):
    logger.debug(f"Encoding GSM message: {plaintext}")
    res = bytearray()
    for c in plaintext:
        res.append(0)
        idx = gsm.find(c)
        if idx != -1:
            res.append(idx)
            continue
        idx = ext.find(c)
        if idx != -1:
            res.append(27)
            res.append(idx)
    return binascii.hexlify(res)

def clean_control_chars(s):
        """Remove control characters from a string (except newline and tab)."""
        return re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', '', s)

def hex2utf(string):
        """Convert UCS-2 hex string to UTF-8."""
        result = ''
        try:
            for i in range(len(string) // 4):
                result += chr(int(string[i * 4:i * 4 + 4], 16))
        except Exception as e:
            logger.warning(f"Failed to decode UCS2 string: {string} - Error: {e}")
        return result

class zteRouter:
    def __init__(self, ip, username, password):
        self.ip = ip
        self.protocol = "http"  # default to http
        self.username = username
        self.password = password
        self.cookies = {}  # Existing cookie management
        self.stok = None
        self.session_expiry = datetime.min  # Initialize expiry in the past
        logger.info(f"Initializing ZTE Router with IP {ip}, Username: {username}, Password: {password}")
        self.try_set_protocol()
        self.referer = f"{self.protocol}://{self.ip}/"
    
    SESSION_FILE = os.path.join("/tmp", "zte_session.json")
    CERT_FILE = "/tmp/zte_router_cert.pem"
    
    def save_session(self):
        session_data = {
            'stok': self.stok,
            'session_expiry': self.session_expiry.isoformat(),
            'cookies': self.cookies
        }
        with open(self.SESSION_FILE, 'w') as f:
            json.dump(session_data, f)
        logger.info("Session saved to disk.")
    
    def load_session(self):
        if os.path.exists(self.SESSION_FILE):
            with open(self.SESSION_FILE, 'r') as f:
                session_data = json.load(f)
                self.stok = session_data.get('stok')
                self.cookies = session_data.get('cookies', {})
                self.session_expiry = datetime.fromisoformat(session_data['session_expiry'])
                logger.info("Session loaded from disk.")
        else:
            logger.info("No existing session file found.")

    def is_session_valid(self):
        return self.stok is not None and datetime.now() < self.session_expiry
    
    def invalidate_session(self):
        logger.info("Invalidating session cookie")
        self.stok = None
        self.session_expiry = datetime.min

    def request_with_session(self, method, url, headers=None, body=None):
        if headers is None:
            headers = {}
        cookie_header = self.build_cookie_header()
        if cookie_header:
            headers['Cookie'] = cookie_header

        response = s.request(method, url, headers=headers, body=body)

        # Detect invalid session or router unavailability
        if response.status in [401, 403] or 'error' in response.data.decode('utf-8').lower():
            logger.warning(f"Session invalid detected (status {response.status}), re-logging in.")
            self.invalidate_session()
            self.getCookie(self.username, self.password, self.get_LD())  # renew session
            # Retry the request after re-login
            cookie_header = self.build_cookie_header()
            if cookie_header:
                headers['Cookie'] = cookie_header
            response = s.request(method, url, headers=headers, body=body)
        elif response.status in [502, 503, 504] or response.status >= 520:
            logger.error(f"Router unavailable or not responding (status {response.status}). Please check router connectivity.")
            raise ConnectionError(f"Router unavailable or not responding (status {response.status})")

        # Update cookies after request
        set_cookie_header = response.headers.get('Set-Cookie', '')
        self.update_cookies(set_cookie_header)

        return response
        
    def get_certificate_info(self, hostname, port=443):
        logger.debug(f"Checking for existing SSL certificate file: {self.CERT_FILE}")
        if os.path.exists(self.CERT_FILE):
            with open(self.CERT_FILE, 'r') as cert_file:
                pem_cert = cert_file.read()
            logger.info(f"Loaded SSL certificate from disk: {self.CERT_FILE}")
            self.parse_certificate(pem_cert)
            return pem_cert

        logger.debug(f"Retrieving SSL certificate for {hostname}:{port}")
        context = ssl._create_unverified_context()
        context.check_hostname = False
        try:
            with socket.create_connection((hostname, port), timeout=5) as sock:
                with context.wrap_socket(sock, server_hostname=hostname) as ssock:
                    der_cert = ssock.getpeercert(binary_form=True)
                    if der_cert is None:
                        logger.error("No certificate received")
                        return None
                    pem_cert = ssl.DER_cert_to_PEM_cert(der_cert)
                    with open(self.CERT_FILE, 'w') as cert_file:
                        cert_file.write(pem_cert)
                    logger.info(f"SSL certificate for {hostname}:{port} retrieved and stored successfully")
                    self.parse_certificate(pem_cert)
                    return pem_cert
        except Exception as e:
            logger.error(f"Failed to retrieve SSL certificate for {hostname}:{port}: {e}")
            return None
        
    def parse_certificate(self, pem_cert):
        logger.debug("Parsing PEM certificate")
        try:
            cert = x509.load_pem_x509_certificate(pem_cert.encode('utf-8'), default_backend())

            def get_attribute(name, oid):
                try:
                    return name.get_attributes_for_oid(oid)[0].value
                except IndexError:
                    return "N/A"

            subject = cert.subject
            issuer = cert.issuer

            subject_info = {
                "Common Name": get_attribute(subject, x509.NameOID.COMMON_NAME),
                "Organization": get_attribute(subject, x509.NameOID.ORGANIZATION_NAME),
                "Organizational Unit": get_attribute(subject, x509.NameOID.ORGANIZATIONAL_UNIT_NAME),
                "Country": get_attribute(subject, x509.NameOID.COUNTRY_NAME),
                "State/Province": get_attribute(subject, x509.NameOID.STATE_OR_PROVINCE_NAME),
                "Email Address": get_attribute(subject, x509.NameOID.EMAIL_ADDRESS),
            }

            issuer_info = {
                "Common Name": get_attribute(issuer, x509.NameOID.COMMON_NAME),
                "Organization": get_attribute(issuer, x509.NameOID.ORGANIZATION_NAME),
                "Organizational Unit": get_attribute(issuer, x509.NameOID.ORGANIZATIONAL_UNIT_NAME),
                "Country": get_attribute(issuer, x509.NameOID.COUNTRY_NAME),
                "State/Province": get_attribute(issuer, x509.NameOID.STATE_OR_PROVINCE_NAME),
                "Email Address": get_attribute(issuer, x509.NameOID.EMAIL_ADDRESS),
            }

            validity = {
                "Not Before": cert.not_valid_before_utc,
                "Not After": cert.not_valid_after_utc,
            }

            serial_number = cert.serial_number

            if log_certificate_details:
                cert_info = "\nCertificate Information:\n========================"
                cert_info += "\nSubject:"
                for key, value in subject_info.items():
                    cert_info += f"\n    {key:22}: {value}"

                cert_info += "\n\nIssuer:"
                for key, value in issuer_info.items():
                    cert_info += f"\n    {key:22}: {value}"

                cert_info += "\n\nValidity:"
                for key, value in validity.items():
                    cert_info += f"\n    {key:22}: {value}"

                cert_info += f"\n\nSerial Number:            {serial_number}"
                logger.info(cert_info)
            else:
                logger.debug("Certificate parsed successfully, logging is disabled.")

        except Exception as e:
            logger.error(f"Failed to parse certificate: {e}")


    def update_cookies(self, set_cookie_header):
        if set_cookie_header:
            cookie = SimpleCookie()
            cookie.load(set_cookie_header)
            for key, morsel in cookie.items():
                self.cookies[key] = morsel.value

    def build_cookie_header(self):
        cookie_header = '; '.join(f'{key}={value}' for key, value in self.cookies.items())
        return cookie_header

    def try_set_protocol(self):
        protocols = ["https", "http"]
        for protocol in protocols:
            url = f"{protocol}://{self.ip}/index.html"
            try:
                response = s.request('GET', url, timeout=2, retries=2)  # reduced timeout and retries
                if response.status in [200, 302, 301]:
                    self.protocol = protocol
                    logger.info(f"Protocol set to {protocol}")
                    if protocol == "https":
                        self.get_certificate_info(self.ip)
                    return
            except Exception as e:
                logger.info(f"Failed to connect using {protocol}: {e}, trying next protocol.")

        # Instead of raising, handle router unavailability gracefully:
        logger.warning("Router is unavailable, protocol not set.")
        self.protocol = None


    def hash(self, str):
        hashed = hashlib.sha256(str.encode()).hexdigest()
        logger.debug(f"Hashed string: {hashed}")
        return hashed

    def getVersion(self):
        logger.debug("Fetching router version")
        header = {"Referer": self.referer}
        # Include cookies if any
        cookie_header = self.build_cookie_header()
        if cookie_header:
            header['Cookie'] = cookie_header
        payload = "isTest=false&cmd=wa_inner_version"
        url = self.referer + f"goform/goform_get_cmd_process?{payload}"
        try:
            r = self.request_with_session('GET', url, headers=header)
            data = r.data.decode('utf-8')
            # Update cookies
            set_cookie_header = r.headers.get('Set-Cookie', '')
            self.update_cookies(set_cookie_header)
            version = json.loads(data)["wa_inner_version"]
            logger.info(f"Router version: {version}")
            return version
        except Exception as e:
            logger.error(f"Failed to fetch version: {e}")
            return ""

    def get_LD(self):
        logger.debug("Fetching LD value")
        header = {"Referer": self.referer}
        # Include cookies if any
        cookie_header = self.build_cookie_header()
        if cookie_header:
            header['Cookie'] = cookie_header
        payload = "isTest=false&cmd=LD"
        url = self.referer + f"goform/goform_get_cmd_process?{payload}"
        try:
            r = self.request_with_session('GET', url, headers=header)
            data = r.data.decode('utf-8')
            # Update cookies
            set_cookie_header = r.headers.get('Set-Cookie', '')
            self.update_cookies(set_cookie_header)
            ld = json.loads(data)["LD"].upper()
            logger.info(f"LD: {ld}")
            return ld
        except Exception as e:
            logger.error(f"Failed to fetch LD: {e}")
            return ""

    def getCookie(self, username, password, LD):
        logger.debug(f"Getting cookie for username: {username}, password: {password}, LD: {LD}")

        self.load_session()
        if self.is_session_valid():
            logger.info("Reusing existing session cookie")
            return self.stok

        header = {"Referer": self.referer}
        cookie_header = self.build_cookie_header()
        if cookie_header:
            header['Cookie'] = cookie_header

        hashPassword = self.hash(password).upper()
        ztePass = self.hash(hashPassword + LD).upper()

        AD = self.get_AD()

        if username:
            goform_id = 'LOGIN_MULTI_USER'
            payload = {
                'isTest': 'false',
                'goformId': goform_id,
                'user': username,
                'password': ztePass,
                'AD': AD
            }
        else:
            goform_id = 'LOGIN'
            payload = {
                'isTest': 'false',
                'goformId': goform_id,
                'password': ztePass
            }

        url = self.referer + "goform/goform_set_cmd_process"
        encoded_payload = urllib.parse.urlencode(payload)
        body = encoded_payload.encode('utf-8')

        try:
            r = self.request_with_session('POST', url, headers=header, body=body)
            set_cookie_header = r.headers.get('Set-Cookie', '')
            self.update_cookies(set_cookie_header)

            stok = self.cookies.get('stok')
            if not stok:
                logger.error("Failed to obtain a valid cookie from the router")
                raise ValueError("Failed to obtain a valid cookie from the router")

            # Set session expiry (e.g., valid for 60 minutes)
            self.session_expiry = datetime.now() + timedelta(minutes=60)
            self.stok = stok
            self.save_session()
            logger.info(f"Obtained new session cookie: stok={stok}")
            return stok

        except Exception as e:
            logger.error(f"Failed to obtain cookie: {e}")
            raise


    def get_RD(self):
        logger.debug("Fetching RD value")
        header = {"Referer": self.referer}
        # Include cookies if any
        cookie_header = self.build_cookie_header()
        if cookie_header:
            header['Cookie'] = cookie_header
        payload = "isTest=false&cmd=RD"
        url = self.referer + f"goform/goform_get_cmd_process?{payload}"
        try:
            r = self.request_with_session('POST', url, headers=header)
            data = r.data.decode('utf-8')
            # Update cookies
            set_cookie_header = r.headers.get('Set-Cookie', '')
            self.update_cookies(set_cookie_header)
            rd = json.loads(data)["RD"]
            logger.info(f"RD: {rd}")
            return rd
        except Exception as e:
            logger.error(f"Failed to fetch RD: {e}")
            return ""

    def get_AD(self):
        logger.debug("Calculating AD value")
        def md5(s):
            m = hashlib.md5()
            m.update(s.encode("utf-8"))
            return m.hexdigest()

        def sha256(s):
            m = hashlib.sha256()
            m.update(s.encode("utf-8"))
            return m.hexdigest().upper()  # .upper() to match your example hash

        wa_inner_version = self.getVersion()
        if wa_inner_version == "":
            return ""

        is_mc888 = "MC888" in wa_inner_version
        is_mc889 = "MC889" in wa_inner_version

        hash_function = sha256 if is_mc888 or is_mc889 else md5

        cr_version = ""  # You need to define or get cr_version value as it's not provided in the given code

        a = hash_function(wa_inner_version + cr_version)

        header = {"Referer": self.referer}
        cookie_header = self.build_cookie_header()
        if cookie_header:
            header['Cookie'] = cookie_header
        try:
            rd_url = self.referer + "goform/goform_get_cmd_process?isTest=false&cmd=RD"
            rd_response = self.request_with_session('GET', rd_url, headers=header)
            data = rd_response.data.decode('utf-8')
            # Update cookies
            set_cookie_header = rd_response.headers.get('Set-Cookie', '')
            self.update_cookies(set_cookie_header)
            rd_json = json.loads(data)
            u = rd_json.get("RD", "")

            result = hash_function(a + u)
            logger.info(f"AD: {result}")
            return result
        except Exception as e:
            logger.error(f"Failed to calculate AD: {e}")
            return ""

    def sendsms(self, phone_number, message):
        logger.debug(f"Sending SMS to {phone_number} with message: {message}")
        try:
            self.getCookie(username=self.username, password=self.password, LD=self.get_LD())
            AD = self.get_AD()
            header = {"Referer": self.referer}
            # Build Cookie header
            cookie_header = self.build_cookie_header()
            if cookie_header:
                header['Cookie'] = cookie_header

            # Encode phone number and message
            phoneNumberEncoded = urllib.parse.quote(phone_number, safe="")
            messageEncoded = gsm_encode(message).decode()

            payload = {
                'isTest': 'false',
                'goformId': 'SEND_SMS',
                'notCallback': 'true',
                'Number': phoneNumberEncoded,
                'sms_time': get_sms_time(),
                'MessageBody': messageEncoded,
                'ID': '-1',
                'encode_type': 'GSM7_default',
                'AD': AD
            }

            # Prepare the encoded form data
            encoded_payload = urllib.parse.urlencode(payload)
            body = encoded_payload.encode('utf-8')

            r = self.request_with_session('POST', self.referer + "goform/goform_set_cmd_process", headers=header, body=body)

            logger.info(f"SMS sent with status code: {r.status}")
            return r.status
        except Exception as e:
            logger.error(f"Failed to send SMS: {e}")
            return None


    def zteinfo(self):
        logger.debug("Fetching ZTE info")
        try:
            # Get LD and update cookies
            LD = self.get_LD()
            # getCookie will update self.cookies
            self.getCookie(username=self.username, password=self.password, LD=LD)
            header = {
                "Host": self.ip,
                "Referer": f"{self.referer}index.html",
            }
            cookie_header = self.build_cookie_header()
            if cookie_header:
                header['Cookie'] = cookie_header
            cmd_url = f"{self.protocol}://{self.ip}/goform/goform_get_cmd_process?isTest=false&cmd=wa_inner_version%2Ccr_version%2Cnetwork_type%2Crssi%2Crscp%2Crmcc%2Crmnc%2Cenodeb_id%2Clte_rsrq%2Clte_rsrp%2CZ5g_snr%2CZ5g_rsrp%2CZCELLINFO_band%2CZ5g_dlEarfcn%2Clte_ca_pcell_arfcn%2Clte_ca_pcell_band%2Clte_ca_scell_band%2Clte_ca_pcell_bandwidth%2Clte_ca_scell_info%2Clte_ca_scell_bandwidth%2Cwan_lte_ca%2Clte_pci%2CZ5g_CELL_ID%2CZ5g_SINR%2Ccell_id%2Cwan_lte_ca%2Clte_ca_pcell_band%2Clte_ca_pcell_bandwidth%2Clte_ca_scell_band%2Clte_ca_scell_bandwidth%2Clte_ca_pcell_arfcn%2Clte_ca_scell_arfcn%2Clte_multi_ca_scell_info%2Cwan_active_band%2Cnr5g_pci%2Cnr5g_action_band%2Cnr5g_cell_id%2Clte_snr%2Cecio%2Cwan_active_channel%2Cnr5g_action_channel%2Cngbr_cell_info%2Cmonthly_tx_bytes%2Cmonthly_rx_bytes%2Clte_pci%2Clte_pci_lock%2Clte_earfcn_lock%2Cwan_ipaddr%2Cwan_apn%2Cpm_sensor_mdm%2Cpm_modem_5g%2Cnr5g_pci%2Cnr5g_action_channel%2Cnr5g_action_band%2CZ5g_SINR%2CZ5g_rsrp%2Cwan_active_band%2Cwan_active_channel%2Cwan_lte_ca%2Clte_multi_ca_scell_info%2Ccell_id%2Cdns_mode%2Cprefer_dns_manual%2Cstandby_dns_manual%2Cnetwork_type%2Crmcc%2Crmnc%2Clte_rsrq%2Clte_rssi%2Clte_rsrp%2Clte_snr%2Cwan_lte_ca%2Clte_ca_pcell_band%2Clte_ca_pcell_bandwidth%2Clte_ca_scell_band%2Clte_ca_scell_bandwidth%2Clte_ca_pcell_arfcn%2Clte_ca_scell_arfcn%2Cwan_ipaddr%2Cstatic_wan_ipaddr%2Copms_wan_mode%2Copms_wan_auto_mode%2Cppp_status%2Cloginfo%2Crealtime_time%2Csignalbar&multi_data=1"
            response = self.request_with_session('GET', cmd_url, headers=header)
            data = response.data.decode('utf-8')
            # Update cookies
            set_cookie_header = response.headers.get('Set-Cookie', '')
            self.update_cookies(set_cookie_header)
            logger.info("Fetched ZTE info successfully")
            return data
        except Exception as e:
            logger.error(f"Failed to fetch ZTE info: {e}")
            return ""

    # ... (Previous methods in the class)

    def zteinfo2(self):
        logger.debug("Fetching ZTE info 2")
        try:
            # Get LD and update cookies
            LD = self.get_LD()
            # getCookie will update self.cookies
            self.getCookie(username=self.username, password=self.password, LD=LD)
            header = {
                "Host": self.ip,
                "Referer": f"{self.referer}index.html",
            }
            cookie_header = self.build_cookie_header()
            if cookie_header:
                header['Cookie'] = cookie_header
            cmd_url = f"{self.protocol}://{self.ip}/goform/goform_get_cmd_process?multi_data=1&isTest=false&sms_received_flag_flag=0&sts_received_flag_flag=0&cmd=network_type%2Crssi%2Clte_rssi%2Crscp%2Clte_rsrp%2CZ5g_snr%2CZ5g_rsrp%2CZCELLINFO_band%2CZ5g_dlEarfcn%2Clte_ca_pcell_arfcn%2Clte_ca_pcell_band%2Clte_ca_scell_band%2Clte_ca_pcell_bandwidth%2Clte_ca_scell_info%2Clte_ca_scell_bandwidth%2Cwan_lte_ca%2Clte_pci%2CZ5g_CELL_ID%2CZ5g_SINR%2Ccell_id%2Cwan_lte_ca%2Clte_ca_pcell_band%2Clte_ca_pcell_bandwidth%2Clte_ca_scell_band%2Clte_ca_scell_bandwidth%2Clte_ca_pcell_arfcn%2Clte_ca_scell_arfcn%2Clte_multi_ca_scell_info%2Cwan_active_band%2Cnr5g_pci%2Cnr5g_action_band%2Cnr5g_cell_id%2Clte_snr%2Cecio%2Cwan_active_channel%2Cnr5g_action_channel%2Cmodem_main_state%2Cpin_status%2Copms_wan_mode%2Copms_wan_auto_mode%2Cloginfo%2Cnew_version_state%2Ccurrent_upgrade_state%2Cis_mandatory%2Cwifi_dfs_status%2Cbattery_value%2Cppp_dial_conn_fail_counter%2Cwifi_chip1_ssid1_auth_mode%2Cwifi_chip2_ssid1_auth_mode%2Csignalbar%2Cnetwork_type%2Cnetwork_provider%2Cppp_status%2Csimcard_roam%2Cspn_name_data%2Cspn_b1_flag%2Cspn_b2_flag%2Cwifi_onoff_state%2Cwifi_chip1_ssid1_ssid%2Cwifi_chip2_ssid1_ssid%2Cwan_lte_ca%2Cmonthly_tx_bytes%2Cmonthly_rx_bytes%2Cpppoe_status%2Cdhcp_wan_status%2Cstatic_wan_status%2Crmcc%2Crmnc%2Cmdm_mcc%2Cmdm_mnc%2CEX_SSID1%2Csta_ip_status%2CEX_wifi_profile%2Cm_ssid_enable%2CRadioOff%2Cwifi_chip1_ssid1_access_sta_num%2Cwifi_chip2_ssid1_access_sta_num%2Clan_ipaddr%2Cstation_mac%2Cwifi_access_sta_num%2Cbattery_charging%2Cbattery_vol_percent%2Cbattery_pers%2Crealtime_tx_bytes%2Crealtime_rx_bytes%2Crealtime_time%2Crealtime_tx_thrpt%2Crealtime_rx_thrpt%2Cmonthly_time%2Cdate_month%2Cdata_volume_limit_switch%2Cdata_volume_limit_size%2Cdata_volume_alert_percent%2Cdata_volume_limit_unit%2Croam_setting_option%2Cupg_roam_switch%2Cssid%2Cwifi_enable%2Cwifi_5g_enable%2Ccheck_web_conflict%2Cdial_mode%2Cprivacy_read_flag%2Cis_night_mode%2Cvpn_conn_status%2Cwan_connect_status%2Csms_received_flag%2Csts_received_flag%2Csms_unread_num%2Cwifi_chip1_ssid2_access_sta_num%2Cwifi_chip2_ssid2_access_sta_num&multi_data=1"
            response = self.request_with_session('GET', cmd_url, headers=header)
            data = response.data.decode('utf-8')
            # Update cookies
            set_cookie_header = response.headers.get('Set-Cookie', '')
            self.update_cookies(set_cookie_header)
            logger.info("Fetched ZTE info 2 successfully")
            return data
        except Exception as e:
            logger.error(f"Failed to fetch ZTE info 2: {e}")
            return ""

    def zteinfo3(self):
        logger.debug("Fetching comprehensive ZTE info (zteinfo3) with hybrid splitting")
        try:
            LD = self.get_LD()
            self.getCookie(username=self.username, password=self.password, LD=LD)

            header = {
                "Host": self.ip,
                "Referer": f"{self.referer}index.html",
            }
            cookie_header = self.build_cookie_header()
            if cookie_header:
                header['Cookie'] = cookie_header

                param_groups = {
                    "system_info": (
                        "wa_inner_version,cr_version,loginfo,new_version_state,current_upgrade_state,"
                        "is_mandatory,modem_main_state,pin_status,signalbar,imei"
                    ),
                    "radio_network": (
                        "network_type,network_provider,network_provider_fullname,rmcc,rmnc,mdm_mcc,mdm_mnc,"
                        "rssi,ecio,ecio_1,ecio_2,ecio_3,ecio_4,rscp,rscp_1,rscp_2,rscp_3,rscp_4,lte_rsrp,"
                        "lte_rsrp_1,lte_rsrp_2,lte_rsrp_3,lte_rsrp_4,lte_rsrq,lte_snr,lte_snr_1,lte_snr_2,lte_snr_3,lte_snr_4,"
                        "lte_rssi,Z5g_rsrp,Z5g_rsrq,Z5g_snr,Z5g_SINR,Z5g_dlEarfcn,Z5g_CELL_ID,ZCELLINFO_band,"
                        "enodeb_id,lte_pci,lte_pci_lock,lte_band,lte_ca_pcell_band,lte_ca_scell_band,lte_ca_scell_info,"
                        "lte_multi_ca_scell_info,lte_multi_ca_scell_sig_info,lte_ca_scell_arfcn,lte_ca_scell_bandwidth,"
                        "lte_ca_pcell_bandwidth,lte_ca_pcell_arfcn,lte_ca_pcell_freq,lte_earfcn_lock,nr5g_action_band,"
                        "nr5g_action_channel,nr5g_action_nsa_band,nr5g_pci,nr5g_cell_id,nr_ca_pcell_band,nr_ca_pcell_freq,"
                        "nr5g_nsa_band_lock,nr5g_sa_band_lock,nr_multi_ca_scell_info,wan_active_band,wan_active_channel,"
                        "cell_id,tx_power,ngbr_cell_info,5g_rx0_rsrp,5g_rx1_rsrp"
                    ),
                    "connectivity": (
                        "wan_ipaddr,wan_apn,wan_connect_status,wan_lte_ca,opms_wan_mode,opms_wan_auto_mode,"
                        "ppp_status,pppoe_status,dial_mode,dhcp_wan_status,static_wan_status,static_wan_ipaddr,"
                        "ip_passthrough_enabled,vpn_conn_status,ppp_dial_conn_fail_counter"
                    ),
                    "wifi": (
                        "wifi_enable,wifi_onoff_state,wifi_5g_enable,wifi_chip_temp,wifi_dfs_status,ssid,EX_SSID1,EX_wifi_profile,"
                        "m_ssid_enable,m_SSID2,wifi_chip1_ssid1_ssid,wifi_chip2_ssid1_ssid,wifi_chip1_ssid1_auth_mode,"
                        "wifi_chip2_ssid1_auth_mode,wifi_chip1_ssid2_access_sta_num,wifi_chip2_ssid2_access_sta_num,"
                        "wifi_chip1_ssid1_access_sta_num,wifi_chip2_ssid1_access_sta_num,wifi_chip1_ssid2_max_access_num,"
                        "wifi_chip2_ssid2_max_access_num,wifi_chip1_ssid1_wifi_coverage,wifi_access_sta_num,sta_ip_status,"
                        "guest_switch"
                    ),
                    "power_sensors": (
                        "battery_value,battery_pers,battery_charging,battery_vol_percent,pm_modem_5g,pm_sensor_5g,"
                        "pm_sensor_mdm,pm_sensor_ambient,pm_sensor_pa1"
                    ),
                    "misc": (
                        "monthly_rx_bytes,monthly_tx_bytes,monthly_time,realtime_rx_bytes,realtime_tx_bytes,"
                        "realtime_rx_thrpt,realtime_tx_thrpt,realtime_time,date_month,data_volume_limit_switch,"
                        "data_volume_limit_size,data_volume_alert_percent,data_volume_limit_unit,roam_setting_option,"
                        "upg_roam_switch,privacy_read_flag,is_night_mode,check_web_conflict,station_mac,lan_ipaddr,"
                        "sms_received_flag,sms_unread_num,sts_received_flag,spn_name_data,spn_b1_flag,spn_b2_flag,"
                        "simcard_roam,"
                        "flux_realtime_tx_bytes,flux_realtime_rx_bytes,flux_realtime_time,"
                        "flux_realtime_tx_thrpt,flux_realtime_rx_thrpt,"
                        "flux_monthly_rx_bytes,flux_monthly_tx_bytes,flux_monthly_time,"
                        "flux_data_volume_limit_size,flux_data_volume_alert_percent,flux_data_volume_limit_unit"
                    ),
                    "dns_config": (
                        "dns_mode,prefer_dns_manual,standby_dns_manual"
                    ),
                    "unclassified": (
                        "RadioOff,apn_interface_version,bandwidth"
                    )
                }


            combined_data = {}
            url_base = f"{self.protocol}://{self.ip}/goform/goform_get_cmd_process?isTest=false&multi_data=1&cmd="

            for group_name, param_str in param_groups.items():
                param_list = param_str.split(',')

                max_params_per_request = 60
                chunks = [
                    param_list[i:i + max_params_per_request]
                    for i in range(0, len(param_list), max_params_per_request)
                ]

                for idx, chunk in enumerate(chunks):
                    cmd_encoded = quote(",".join(chunk))
                    url = url_base + cmd_encoded

                    response = self.request_with_session('GET', url, headers=header)
                    raw_data = response.data.decode('utf-8')

                    try:
                        parsed = json.loads(raw_data)
                        combined_data.update(parsed)
                        logger.debug(f"Fetched {group_name} (chunk {idx+1}) with {len(parsed)} values")
                    except Exception as ex:
                        logger.warning(f"Failed to parse {group_name} (chunk {idx+1}): {ex}")
                        continue

                    set_cookie_header = response.headers.get('Set-Cookie', '')
                    self.update_cookies(set_cookie_header)

            logger.info("Fetched comprehensive ZTE info successfully using hybrid strategy")
            return json.dumps(combined_data)

        except Exception as e:
            logger.error(f"Failed to fetch comprehensive ZTE info: {e}")
            return ""

    def zteinfo4(self):
        """
        Fetch ZTE modem info for both 'station_list' (WiFi) and 'lan_station_list' (LAN),
        tag each with type, and return a merged list under 'all_devices'.

        Returns:
            str: JSON string of the combined result, or empty string on error.
        """
        logger.debug("Fetching ZTE info: station_list and lan_station_list (tagged & merged)")
        try:
            LD = self.get_LD()
            self.getCookie(username=self.username, password=self.password, LD=LD)

            header = {
                "Host": self.ip,
                "Referer": f"{self.referer}index.html",
            }

            cookie_header = self.build_cookie_header()
            if cookie_header:
                header['Cookie'] = cookie_header

            combined_data = {}

            for param in ["station_list", "lan_station_list"]:
                cmd_str = quote(param)
                url = f"{self.protocol}://{self.ip}/goform/goform_get_cmd_process?isTest=false&cmd={cmd_str}"
                response = self.request_with_session('GET', url, headers=header)
                raw_data = response.data.decode('utf-8')

                set_cookie_header = response.headers.get('Set-Cookie', '')
                self.update_cookies(set_cookie_header)

                try:
                    parsed = json.loads(raw_data)

                    # Tag each device with type: WiFi or LAN
                    for item in parsed.get(param, []):
                        item["type"] = "WiFi" if param == "station_list" else "LAN"

                    combined_data[param] = parsed.get(param, [])
                    logger.debug(f"Fetched {param} with {len(parsed.get(param, []))} entries")
                except Exception as ex:
                    logger.warning(f"Failed to parse {param}: {ex}")
                    combined_data[param] = []

            # ✅ Merge all into one list
            combined_data["all_devices"] = combined_data["station_list"] + combined_data["lan_station_list"]
            logger.info(f"Fetched and merged {len(combined_data['all_devices'])} total devices")

            return json.dumps(combined_data)

        except Exception as e:
            logger.error(f"Failed to fetch ZTE info: {e}")
            return ""


    def ztesmsinfo(self):
        logger.debug("Fetching ZTE SMS info")
        try:
            # Get LD and update cookies
            LD = self.get_LD()
            # getCookie will update self.cookies
            self.getCookie(username=self.username, password=self.password, LD=LD)
            header = {
                "Host": self.ip,
                "Referer": f"{self.referer}index.html",
            }
            cookie_header = self.build_cookie_header()
            if cookie_header:
                header['Cookie'] = cookie_header
            cmd_url = f"{self.protocol}://{self.ip}/goform/goform_get_cmd_process?isTest=false&cmd=sms_capacity_info"
            response = self.request_with_session('GET', cmd_url, headers=header)
            data = response.data.decode('utf-8')
            # Update cookies
            set_cookie_header = response.headers.get('Set-Cookie', '')
            self.update_cookies(set_cookie_header)

            # Parse the response JSON
            data_json = json.loads(data)
            logger.info("Fetched ZTE SMS info successfully")

            # Calculate sms_capacity_left
            sms_nv_total = int(data_json.get("sms_nv_total", 0))
            sms_nv_rev_total = int(data_json.get("sms_nv_rev_total", 0))
            sms_nv_send_total = int(data_json.get("sms_nv_send_total", 0))
            sms_capacity_left = sms_nv_total - sms_nv_rev_total - sms_nv_send_total

            # Add the new key-value pair
            data_json["sms_capacity_left"] = str(sms_capacity_left)
            return json.dumps(data_json)
        except Exception as e:
            logger.error(f"Failed to fetch SMS info: {e}")
            return ""

    def ztereboot(self):
        logger.debug("Rebooting ZTE router")
        try:
            # Get LD and update cookies
            LD = self.get_LD()
            # getCookie will update self.cookies
            self.getCookie(username=self.username, password=self.password, LD=LD)
            # AD value
            AD = self.get_AD()
            header = {"Referer": self.referer}
            cookie_header = self.build_cookie_header()
            if cookie_header:
                header['Cookie'] = cookie_header
            payload = {
                'isTest': 'false',
                'goformId': 'REBOOT_DEVICE',
                'AD': AD
            }
            encoded_payload = urllib.parse.urlencode(payload)
            body = encoded_payload.encode('utf-8')
            r = self.request_with_session('POST', self.referer + "goform/goform_set_cmd_process", headers=header, body=body)
            logger.info(f"Router rebooted with status code: {r.status}")
            return r.status
        except Exception as e:
            logger.error(f"Failed to reboot router: {e}")
            return None

    def deletesms(self, msg_id):
        logger.debug(f"Deleting SMS with ID: {msg_id}")
        try:
            # Get LD and update cookies
            LD = self.get_LD()
            # getCookie will update self.cookies
            self.getCookie(username=self.username, password=self.password, LD=LD)
            # AD value
            AD = self.get_AD()
            header = {"Referer": self.referer}
            cookie_header = self.build_cookie_header()
            if cookie_header:
                header['Cookie'] = cookie_header
            payload = {
                'isTest': 'false',
                'goformId': 'DELETE_SMS',
                'msg_id': msg_id,
                'AD': AD
            }
            encoded_payload = urllib.parse.urlencode(payload)
            body = encoded_payload.encode('utf-8')
            r = self.request_with_session('POST', self.referer + "goform/goform_set_cmd_process", headers=header, body=body)
            logger.info(f"SMS deleted with status code: {r.status}")
            return r.status
        except Exception as e:
            logger.error(f"Failed to delete SMS: {e}")
            return None

    def parsesms(self):
        logger.debug("Starting SMS parsing process")
        try:
            LD = self.get_LD()
            self.getCookie(username=self.username, password=self.password, LD=LD)

            header = {"Referer": self.referer}
            cookie_header = self.build_cookie_header()
            if cookie_header:
                header['Cookie'] = cookie_header

            payload = {
                'cmd': 'sms_data_total',
                'page': '0',
                'data_per_page': '5000',
                'mem_store': '1',
                'tags': '10',
                'order_by': 'order by id desc'
            }

            encoded_payload = urllib.parse.urlencode(payload)
            url = self.referer + "goform/goform_get_cmd_process?" + encoded_payload

            r = self.request_with_session('GET', url, headers=header)
            response_text = r.data.decode('utf-8', errors='replace')

            logger.debug(f"Raw SMS response: {repr(response_text[:300])}...")  # Preview first 300 chars

            # Clean invalid characters
            sanitized_text = clean_control_chars(response_text)
            sanitized_text = sanitized_text.replace('HR�Telekom', 'HR Telekom')  # Specific fix

            try:
                response_json = json.loads(sanitized_text)
            except json.JSONDecodeError as e:
                logger.error(f"JSON decoding failed: {e}")
                return ""

            # Ensure 'messages' exists and is a list
            if 'messages' not in response_json or not isinstance(response_json['messages'], list):
                logger.warning("'messages' key is missing or invalid in SMS response; defaulting to empty list.")
                response_json['messages'] = []

            messages = response_json['messages']
            logger.info(f"Fetched {len(messages)} SMS messages")

            decode_errors = 0
            for item in messages:
                try:
                    original_content = item.get('content', '')
                    decoded = hex2utf(original_content)
                    item['content'] = decoded
                except Exception as e:
                    logger.warning(f"Failed to decode SMS content: {e}")
                    decode_errors += 1

            if decode_errors:
                logger.warning(f"{decode_errors} messages failed to decode cleanly.")

            # Return dummy message if no SMS exists
            if not messages:
                dummy_message = {
                    'id': '999',
                    'number': 'DUMMY',
                    'content': 'NO SMS IN MEMORY',
                    'tag': '1',
                    'date': datetime.now().strftime('%y,%m,%d,%H,%M,%S,+2'),
                    'draft_group_id': '',
                    'received_all_concat_sms': '1',
                    'concat_sms_total': '0',
                    'concat_sms_received': '0',
                    'sms_class': '4'
                }
                response_json['messages'].append(dummy_message)

            logger.info("Parsed all SMS messages successfully")
            return json.dumps(response_json, indent=2)

        except Exception as e:
            logger.error(f"Failed to parse SMS: {e}")
            return ""


    def connect_data(self):
        logger.debug("Connecting to data network")
        try:
            # Get LD and update cookies
            LD = self.get_LD()
            # getCookie will update self.cookies
            self.getCookie(username=self.username, password=self.password, LD=LD)
            # AD value
            AD = self.get_AD()
            header = {"Referer": self.referer}
            cookie_header = self.build_cookie_header()
            if cookie_header:
                header['Cookie'] = cookie_header
            payload = {
                'isTest': 'false',
                'goformId': 'CONNECT_NETWORK',
                'AD': AD
            }
            encoded_payload = urllib.parse.urlencode(payload)
            body = encoded_payload.encode('utf-8')
            r = self.request_with_session('POST', self.referer + "goform/goform_set_cmd_process", headers=header, body=body)
            logger.info(f"Connected to data network with status code: {r.status}")
            return r.status
        except Exception as e:
            logger.error(f"Failed to connect to data network: {e}")
            return None

    def disconnect_data(self):
        logger.debug("Disconnecting from data network")
        try:
            # Get LD and update cookies
            LD = self.get_LD()
            # getCookie will update self.cookies
            self.getCookie(username=self.username, password=self.password, LD=LD)
            # AD value
            AD = self.get_AD()
            header = {"Referer": self.referer}
            cookie_header = self.build_cookie_header()
            if cookie_header:
                header['Cookie'] = cookie_header
            payload = {
                'isTest': 'false',
                'goformId': 'DISCONNECT_NETWORK',
                'AD': AD
            }
            encoded_payload = urllib.parse.urlencode(payload)
            body = encoded_payload.encode('utf-8')
            r = self.request_with_session('POST', self.referer + "goform/goform_set_cmd_process", headers=header, body=body)
            logger.info(f"Disconnected from data network with status code: {r.status}")
            return r.status
        except Exception as e:
            logger.error(f"Failed to disconnect from data network: {e}")
            return None

    def setdata_mode(self, BearerPreference):
        logger.debug(f"Setting data mode with BearerPreference: {BearerPreference}")
        try:
            # Get LD and update cookies
            LD = self.get_LD()
            self.getCookie(username=self.username, password=self.password, LD=LD)

            # Get AD value
            AD = self.get_AD()

            # Build headers
            header = {"Referer": self.referer}
            cookie_header = self.build_cookie_header()
            if cookie_header:
                header['Cookie'] = cookie_header

            # Prepare payload
            payload = {
                'isTest': 'false',
                'goformId': 'SET_BEARER_PREFERENCE',
                'BearerPreference': BearerPreference,
                'AD': AD
            }
            encoded_payload = urllib.parse.urlencode(payload)
            body = encoded_payload.encode('utf-8')

            # Send request
            r = self.request_with_session('POST', self.referer + "goform/goform_set_cmd_process", headers=header, body=body)
            logger.info(f"Set data mode '{BearerPreference}' with status code: {r.status}")
            return r.status
        except Exception as e:
            logger.error(f"Failed to set data mode '{BearerPreference}': {e}")
            return None



# Global variables for SMS sending
getsmstime = get_sms_time()
getsmstimeEncoded = urllib.parse.quote(getsmstime, safe="")
#phoneNumber = '13909'  # enter phone number here
#phoneNumberEncoded = urllib.parse.quote(phoneNumber, safe="")
#message = 'BRZINA'  # enter your message here
#messageEncoded = gsm_encode(message)
#outputmessage = messageEncoded.decode()

if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: script.py ip password command [username] [phone_number] [message]")
        sys.exit(1)

    ip = sys.argv[1]
    password = sys.argv[2]
    command = int(sys.argv[3])

    # Initialize variables
    username = None
    phone_number = None
    message = None

    # Adjust argument parsing based on the command
    if command == 8:
        # Command to send SMS
        if len(sys.argv) == 6:
            # No username provided
            username = None
            phone_number = sys.argv[4]
            message = sys.argv[5]
        elif len(sys.argv) == 7:
            # Username provided
            username = sys.argv[4] if sys.argv[4] != "" else None
            phone_number = sys.argv[5]
            message = sys.argv[6]
        else:
            print("Invalid number of arguments for sending SMS")
            sys.exit(1)
    else:
        # For other commands
        if len(sys.argv) > 4:
            username = sys.argv[4] if sys.argv[4] != "" else None

    # Create a router instance
    zte = zteRouter(ip, username, password)

    logger.info(f"Command: {command}, Username: {username}, Password: {password}")

    try:
        result = None  # Ensure result is initialized
        if command == 1:
            result = zte.zteinfo()
            print(result)
        elif command == 2:
            result = zte.zteinfo2()
            print(result)
        elif command == 3:
            result = zte.ztesmsinfo()
            print(result)
        elif command == 4:
            result = zte.ztereboot()
            print(result)
        elif command == 5:
            result = zte.parsesms()
            if result:
                data = json.loads(result)
                ids = [msg['id'] for msg in data['messages']]
                if ids:
                    formatted_ids = ";".join(ids)
                    logger.info(f"Deleting SMS messages with IDs: {formatted_ids}")
                    result = zte.deletesms(formatted_ids)
                    print(result)
                else:
                    logger.info("No SMS in memory")
                    sys.exit(0)
        elif command == 6:
            result = zte.parsesms()
            if result:
                test = json.loads(result)
                if test["messages"]:
                    first_message = test["messages"][0]
                    first_message_json = json.dumps(first_message)
                    print(first_message_json)
                else:
                    dummy_message = {
                        'id': '999',
                        'number': 'DUMMY',
                        'content': 'NO SMS IN MEMORY',
                        'tag': '1',
                        'date': '24,07,18,09,39,05,+8',
                        'draft_group_id': '',
                        'received_all_concat_sms': '1',
                        'concat_sms_total': '0',
                        'concat_sms_received': '0',
                        'sms_class': '4'
                    }
                    print(json.dumps(dummy_message))
                    sys.exit(0)
        elif command == 7:
            result = zte.zteinfo3()
            print(result)
        elif command == 8:
            if phone_number and message:
                logger.info(f"Sending SMS to {phone_number} with message: {message}")
                result = zte.sendsms(phone_number, message)
                print(result)
            else:
                logger.error(f"Phone number or message not provided. Phone number: {phone_number}, Message: {message}")
                print("Phone number or message not provided for sending SMS")
                sys.exit(1)
        elif command == 9:
            result = zte.connect_data()
            print(result)
        elif command == 10:
            result = zte.disconnect_data()
            print(result)
        elif command == 11:
            result = zte.setdata_mode("Only_LTE")
            print(result)
        elif command == 12:
            result = zte.setdata_mode("4G_AND_5G")
            print(result)
        elif command == 13:
            result = zte.setdata_mode("LTE_AND_5G")
            print(result)
        elif command == 14:
            result = zte.setdata_mode("Only_5G")
            print(result)
        elif command == 15:
            result = zte.setdata_mode("WL_AND_5G")
        elif command == 16:
            result = zte.zteinfo4()
            print(result)
        else:
            print(f"Invalid command: {command}")
            sys.exit(1)

    except Exception as e:
        logger.error(f"An error occurred: {e}")