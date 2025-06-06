#!/usr/bin/python3
# -*- coding: utf-8 -*-
r'''
Copyright 2025, Tijl "Photubias" Deneut <@tijldeneut>
This script provides offline decryption of Chromium based browser user data: Google Chrome, Edge Chromium and Opera

Credentials (and cookies) are encrypted using a Browser Master Encryption key.
This BME key is encrypted using DPAPI in the file "Local State", mostly located at
%localappdata%\{Google/Microsoft}\{Chrome/Edge}\User Data
or %appdata%\Opera Software\Opera Stable
This BME key can then be used to decrypt (AES GCM) the login data and cookies, mostly located at
%localappdata%\{Google/Microsoft}\{Chrome/Edge}\User Data\Default\
or %appdata%\Opera Software\Opera Stable\

DPAPI decrypting the BME key is the hard part. It uses the user DPAPI Masterkey secret from a DPAPI Masterkey file (MK file). 
To identify which DPAPI Masterkey file, the browser "Local State" file contains the cleartext GUID, which is the filename of the MK file
Usually this DPAPI MK file is located at
%appdata%\Microsoft\Protect\<SID>\<GUID>
This DPAPI Masterkey secret is 64bytes in length and can be found either encrypted in lsass memory or encrypted inside the above MK file
The secret within the MK file can be decrypted either via Local AD Domain RSA Key or using local user details
- Local User Details are user SID + SHA1 password hash or sometimes user SID + NTLM password hash (on AzureAD only systems there are no local details and lsass is the only way for now)
- AD Domain RSA Key is the PVK export containing details to construct a private/public RSA encryption certificate, having this and the user MK file can decrypt all domain members

## Generating a list of decrypted MK's can be done with mkudec.py:
e.g. mkudec.py %appdata%\Roaming\Microsoft\Protect\<SID>\* -a <hash> | findstr Secret > masterkeylist.txt
#> and remove all strings '    Secret:'

UPDATE 2024-07-23: Since Chrome v127 a new encryption layer ('v20') was introduced called "Application Bound Encryption (ABE)", System DPAPI data required
UPDATE 2025-02-04: Since Chrome v133 the encryption algorithm changed from AES-GCM to ChaCha20-Poly1305 (Thank You @MrMcX)
'''

import argparse, os, json, base64, sqlite3, time, warnings, re
from Crypto.Cipher import AES, ChaCha20_Poly1305
warnings.filterwarnings('ignore')
try:
    from dpapick3 import blob, masterkey, registry
except ImportError:
    raise ImportError('Missing dpapick3, please install via pip install dpapick3')

def parseArgs():
    print('[!] Welcome. To decrypt, one of four combo\'s is required: \n'
          'Decrypted Masterkey / file containing decrypted Masterkeys / MK file, SID and User Pwd or Hash / MK file and Domain PVK\n'
          'Browser data can be found here:\n'
          '%localappdata%\\{Google/Microsoft}\\{Chrome/Edge}\\User Data\\Local State\n'
          'Passwords in subfolder Default\\Login Data\n'
          'Cookies in subfolder Default\\Network\\Cookies\n')
    oParser = argparse.ArgumentParser()
    oParser.add_argument('--statefile', '-t', metavar='FILE', help='Browser Local State file', default='Local State')
    oParser.add_argument('--loginfile', '-l', metavar='FILE', help='Browser Login Data file (optional)')
    oParser.add_argument('--cookies', '-c', metavar='FILE', help='Browser Cookies file (optional)')
    oParser.add_argument('--masterkey', '-k', metavar='HEX', help='Masterkey, 128 HEX Characters or in SHA1 format (optional)')
    oParser.add_argument('--systemmasterkey', '-y', metavar='FOLDER', default=os.path.join('Windows','System32','Microsoft','Protect','S-1-5-18','User'), help=r'System Masterkey folder')
    oParser.add_argument('--masterkeylist', '-f', metavar='FILE', help='File containing one or more masterkeys for mass decryption (optional)')
    oParser.add_argument('--mkfile', '-m', metavar='FILE', help='GUID file or folder to get Masterkey(s) from (optional)')
    oParser.add_argument('--sid', '-s', metavar='SID', help='User SID (optional)')
    oParser.add_argument('--system', '-e', metavar='HIVE', default=os.path.join('Windows','System32','config','SYSTEM'), help='System Registry file (optional)')
    oParser.add_argument('--security', '-u', metavar='HIVE', default=os.path.join('Windows','System32','config','SECURITY'), help='Security Registry file (optional)')
    oParser.add_argument('--pwdhash', '-a', metavar='HASH', help='User password SHA1 hash (optional)')
    oParser.add_argument('--password', '-p', metavar='PASS', help='User password (optional)')
    oParser.add_argument('--pvk', '-r', metavar='FILE', help='AD RSA cert in PVK format (optional)')
    oParser.add_argument('--export', '-o', metavar='FILE', help='CSV file to export credentials to (optional)')
    oParser.add_argument('--verbose', '-v', action = 'store_true', default = False, help='Print decrypted creds/cookies to console (optional)')
    oArgs = oParser.parse_args()

    if not os.path.isfile(oArgs.statefile): exit('[-] Error: Please provide Local State file')
    if oArgs.loginfile and not os.path.isfile(oArgs.loginfile): exit('[-] Error: File not found: ' + oArgs.loginfile)
    if oArgs.cookies and not os.path.isfile(oArgs.cookies): exit('[-] Error: File not found: ' + oArgs.cookies)
    if oArgs.masterkeylist and not os.path.isfile(oArgs.masterkeylist): exit('[-] Error: File not found: ' + oArgs.masterkeylist)
    if oArgs.pvk and not os.path.isfile(oArgs.pvk): exit('[-] Error: File not found: ' + oArgs.pvk)
    if oArgs.mkfile: oArgs.mkfile = oArgs.mkfile.replace('*','')
    if oArgs.mkfile and not os.path.isfile(oArgs.mkfile) and not os.path.isdir(oArgs.mkfile): exit('[-] Error: File/folder not found: ' + oArgs.mkfile)
    if not os.path.isfile(oArgs.system): oArgs.system = None
    if not os.path.isfile(oArgs.security): oArgs.security = None
    if not os.path.isdir(oArgs.systemmasterkey): oArgs.systemmasterkey = None
    if oArgs.mkfile and not oArgs.sid: 
        try:
            oArgs.sid = re.findall(r"S-1-\d+-\d+-\d+-\d+-\d+-\d+", oArgs.mkfile)[0]
            print(f'[+] Detected SID: {oArgs.sid}')
        except: pass
    if oArgs.mkfile and oArgs.sid and not oArgs.password and not oArgs.pwdhash: 
        oArgs.pwdhash = 'da39a3ee5e6b4b0d3255bfef95601890afd80709'
        # On older systems: oArgs.pwdhash = '31d6cfe0d16ae931b73c59d7e0c089c0'
        print('[+] No password data provided, using empty hash')
    if oArgs.pwdhash: oArgs.pwdhash = bytes.fromhex(oArgs.pwdhash)
    return oArgs

def parseLocalState(sLocalStateFile):
    oABESystemBlob = sVersion = None
    try:
        with open(sLocalStateFile, 'r') as oFile: jsonLocalState = json.loads(oFile.read())
        oFile.close()
        bDPAPIBlob = base64.b64decode(jsonLocalState['os_crypt']['encrypted_key'])[5:]
        if 'app_bound_encrypted_key' in jsonLocalState['os_crypt']:
            bABESystemData = base64.b64decode(jsonLocalState['os_crypt']['app_bound_encrypted_key']).strip(b'\x00')
            if bABESystemData[:4] == b'APPB': oABESystemBlob = blob.DPAPIBlob(bABESystemData[4:])
        if 'variations_permanent_consistency_country' in jsonLocalState: sVersion = jsonLocalState['variations_permanent_consistency_country'][0]
    except Exception as e:
        print(f'[-] Error: file {sLocalStateFile} not a (correct) State file')
        print(e)
        return False, oABESystemBlob, sVersion

    oBlob = blob.DPAPIBlob(bDPAPIBlob)
    return oBlob, oABESystemBlob, sVersion

def parseLoginFile(sLoginFile, lstGUIDs):
    lstLogins = []
    oConn = sqlite3.connect(sLoginFile)
    oConn.text_factory = lambda b: b.decode(errors = 'ignore')
    oCursor = oConn.cursor()
    try:
        oCursor.execute('SELECT origin_url, username_value, password_value FROM logins')
        for lstData in oCursor.fetchall():
            #print(lstData[0])
            if lstData[2][:4] == b'\x01\x00\x00\x00': 
                oBlob = blob.DPAPIBlob(lstData[2])
                if not oBlob.mkguid in lstGUIDs: lstGUIDs.append(oBlob.mkguid)
            lstLogins.append((lstData[0], lstData[1], lstData[2]))
    except Exception as e:
        print('[-] Error reading Login Data file, make sure it is not in use.')
        print(e)
    oCursor.close()
    oConn.close()
    
    return lstLogins, lstGUIDs ## lstLogins = list of lists (url, username, blob)

def parseCookieFile(sCookieFile, lstGUIDs):
    lstCookies = []
    oConn = sqlite3.connect(sCookieFile)
    oCursor = oConn.cursor()
    try:
        oCursor.execute('SELECT name, CAST(encrypted_value AS BLOB), host_key, path, is_secure, is_httponly, creation_utc, expires_utc FROM cookies ORDER BY host_key')
        for lstData in oCursor.fetchall():
            if lstData[1][:4] == b'\x01\x00\x00\x00': 
                oBlob = blob.DPAPIBlob(lstData[1])
                if not oBlob.mkguid in lstGUIDs: lstGUIDs.append(oBlob.mkguid)
            lstCookies.append((lstData[0], lstData[1], lstData[2], lstData[3], lstData[4], lstData[5], lstData[6], lstData[7]))
    except Exception as e:
        print('[-] Error reading Cookies file, make sure it is not in use.')
        print(e)
        exit()
    oCursor.close()
    oConn.close()
    
    return lstCookies, lstGUIDs ## lstCookies = list of lists (name, blob, domain, path, secureconnection, httponly, created, expires)

def tryDPAPIDecrypt(oBlob, bMasterkey):
    try: 
        if oBlob.decrypt(bMasterkey): return oBlob.cleartext
    except: pass
    return None

def decryptChromeString(bData, bBMEKey, bABEKey, lstMasterkeys, boolVerbose = False):
    if bData[:4] == b'\x01\x00\x00\x00':
        oBlob = blob.DPAPIBlob(bData)
        for bMK in lstMasterkeys:
            oBlob.decrypt(bMK)
            if oBlob.decrypted: return oBlob.cleartext.decode(errors='ignore')
    elif bData[:3] == b'v20' or bData[:3] == 'v20': ## Version|IV|ciphertext|tag, 3|12|<var>|16 bytes
        ## v20 is encrypted using the app-bound encryption key (ABE)
        bIV = bData[3:15]
        bEncrypted = bData[15:-16]
        bTag = bData[-16:]
        oCipher = AES.new(bABEKey, AES.MODE_GCM, bIV)
        try:
            bDecrypted = oCipher.decrypt_and_verify(bEncrypted, bTag)
        except ValueError as e:
            print(f'[-] Error decrypting Chrome ABE (v20) password: {', '.join(e.args)}')
            return ''
        return bDecrypted.decode(errors='ignore')
    else: ## Version|IV|ciphertext, 4|12|<var>
        try:
            bIV = bData[3:15]
            bEncrypted = bData[15:]
            oCipher = AES.new(bBMEKey, AES.MODE_GCM, bIV)
            bDecrypted = oCipher.decrypt(bEncrypted)
            try: return bDecrypted[:-16].decode()
            except: return ''
            #return bDecrypted[:-16].decode(errors='ignore')
        except: 
            if boolVerbose: print('[-] Error decrypting, maybe Browser Engine < v80')
            pass
    return None

def decryptLogins(lstLogins, bBMEKey, bABEKey,  lstMasterkeys, sCSVFile = None, boolVerbose = False):
    iDecrypted = 0
    if sCSVFile: 
        oFile = open('logins_' + sCSVFile, 'a')
        oFile.write('URL;Username;Password\n')
    for lstLogin in lstLogins:
        sDecrypted = decryptChromeString(lstLogin[2], bBMEKey, bABEKey, lstMasterkeys)
        if boolVerbose: 
                print(f'URL:       {lstLogin[0]}')
                print(f'User Name: {lstLogin[1]}')
                print(f'Password:  {sDecrypted}')
                print('*' * 50)
        if sDecrypted != None: iDecrypted += 1
        if sCSVFile: oFile.write('{};{};{}\n'.format(lstLogin[0], lstLogin[1], sDecrypted))
    if sCSVFile: oFile.close()
    return iDecrypted

def decryptCookies(lstCookies, bBMEKey, bABEKey, lstMasterkeys, sCSVFile = None, boolVerbose = False):
    iDecrypted = 0
    if sCSVFile: 
        oFile = open('cookies_' + sCSVFile, 'a')
        oFile.write('name;value;host_key;path;is_secure;is_httponly;creation_utc;expires_utc\n')
    for lstCookie in lstCookies:
        decryptChromeString(lstCookie[1], bBMEKey, bABEKey, lstMasterkeys)
        try: 
            sDecrypted = decryptChromeString(lstCookie[1], bBMEKey, bABEKey, lstMasterkeys)
            ## Chrome timestamp is "amount of microseconds since 01-01-1601", so we need math
            sCreated = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(lstCookie[6] / 1000000 - 11644473600))
            sExpires = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(lstCookie[7] / 1000000 - 11644473600))
        except:
            continue
        if boolVerbose: 
                print(f'Name:      {lstCookie[0]}')
                print(f'Content:   {sDecrypted}')
                print(f'Domain:    {lstCookie[2]}')
                print(f'Path:      {lstCookie[3]}')
                if lstCookie[4] == 1: print('Send for:  Secure connections only')
                else: print('Send for:  Any kind of connection')
                if lstCookie[5] == 1: print('HttpOnly:  Yes')
                else: print('HttpOnly:  No (Accessible to scripts)')
                print(f'Created:   {sCreated}')
                print(f'Expires:   {sExpires}')
                print('*' * 50)
        if sDecrypted: iDecrypted += 1
        if sCSVFile: oFile.write('{};{};{};{};{};{};{};{}\n'.format(lstCookie[0], sDecrypted, lstCookie[2], lstCookie[3], lstCookie[4], lstCookie[5], lstCookie[6], lstCookie[7]))
    if sCSVFile: oFile.close()
    return iDecrypted

if __name__ == '__main__':
    oArgs = parseArgs()
    lstGUIDs, lstLogins, lstCookies, lstMasterkeys, lstSystemMasterkeys = [], [], [], [], []
    bBrowserBMEKey = bMasterkey = oMKP = oABESystemBlob = oABEUserBlob = bBrowserABEKey = bABEMasterkey = None
    
    ## List required GUID from Local State
    oStateBlob, oABESystemBlob, sVersion = parseLocalState(oArgs.statefile)
    print(f'[+] Browser State File encrypted with Masterkey GUID: {oStateBlob.mkguid}')
    lstGUIDs.append(oStateBlob.mkguid)
    if oABESystemBlob: print(f'    And also the ABE-Key requires the SYSTEM Masterkey with GUID: {oABESystemBlob.mkguid}')
    if sVersion: print(f'    > Detected Browser version: {sVersion}')

    ## Get Logins, if any
    if oArgs.loginfile: 
        lstLogins, lstGUIDs = parseLoginFile(oArgs.loginfile, lstGUIDs)
        print('[!] Found {} credential(s).'.format(str(len(lstLogins))))
    
    ## Get Cookies, if any
    if oArgs.cookies: 
        lstCookies, lstGUIDs = parseCookieFile(oArgs.cookies, lstGUIDs)
        print('[!] Found {} cookie(s).'.format(str(len(lstCookies))))

    ## Decrypting ABE Blob
    if oArgs.system and oArgs.security and oArgs.systemmasterkey and oABESystemBlob:
        print('[+] Found SYSTEM & SECURITY hives, trying first-stage ABE Key decrypting using SYSTEM keys')
        oReg = registry.Regedit()
        oSecrets = oReg.get_lsa_secrets(oArgs.security, oArgs.system)
        bDPAPI_SYSTEM = oSecrets.get('DPAPI_SYSTEM')['CurrVal']
        oMKP1 = masterkey.MasterKeyPool()
        oMKP1.loadDirectory(oArgs.systemmasterkey)
        oMKP1.addSystemCredential(bDPAPI_SYSTEM)
        oMKP1.try_credential_hash(None, None)
        for lstMKL in oMKP1.keys.values():
            for oMK in lstMKL:
                bABEUserData = tryDPAPIDecrypt(oABESystemBlob, oMK.get_key()) 
                if bABEUserData:
                    oABEUserBlob = blob.DPAPIBlob(bABEUserData)
                    print(f'[+] Decrypted first-stage of ABE-Key, needed for second-stage is USER Masterkey with GUID: {oABEUserBlob.mkguid}')
                    lstGUIDs.append(oABEUserBlob.mkguid)
                    break

    ## If no decryption details are provided, feed some results back
    if oABESystemBlob and (not oArgs.system or not oArgs.security or not oArgs.systemmasterkey):
        print(f'[!] Unable to decrypt the ABE blob, please specify System & Security hives and Masterkey with GUID {oABESystemBlob.mkguid}')
        exit(0)
    if not oArgs.masterkey and not oArgs.masterkeylist and not oArgs.mkfile: 
        if(len(lstGUIDs) > 1):
            lstGUIDs.sort()
            print('[!] Required for full decryption are {} different Masterkeys, their GUIDs:'.format(str(len(lstGUIDs))) )
            for sGUID in lstGUIDs: print(f'    {sGUID}')
        print('[!] Go and find these files and accompanying decryption details')
        exit(0)
    
    print('\n ----- Getting Browser (& ABE) Master Encryption Key -----')
    ## Option 1 for getting BME Key: the 64byte DPAPI masterkey is provided (either directly or via a list)
    if oArgs.masterkey: 
        print('[!] Trying direct masterkey')
        bMasterkey = bytes.fromhex(oArgs.masterkey)
    elif oArgs.masterkeylist:
        print('[!] Trying list of masterkeys')
        for sMasterkey in open(oArgs.masterkeylist,'r').read().splitlines(): 
            if len(sMasterkey.strip()) == 128 or len(sMasterkey.strip()) == 40: lstMasterkeys.append(bytes.fromhex(sMasterkey.strip()))
        for bMK in lstMasterkeys:
            bBrowserBMEKey = tryDPAPIDecrypt(oStateBlob, bMK)
            if oABEUserBlob: bBrowserABEKey = tryDPAPIDecrypt(oABEUserBlob, bMK)
    ##  All other options require one or more MK files, using MK Pool
    if oArgs.mkfile:
        oMKP = masterkey.MasterKeyPool()
        if os.path.isfile(oArgs.mkfile): oMKP.addMasterKey(open(oArgs.mkfile,'rb').read())
        else: 
            oMKP.loadDirectory(oArgs.mkfile)
            if oArgs.verbose: print('[!] Imported {} keys'.format(str(len(list(oMKP.keys)))))
    
    ## Option 2 for getting BME Key: the PVK domain key to decrypt the MK key
    if oMKP and oArgs.pvk:
        print('[!] Try MK decryption with the PVK domain key')
        if oMKP.try_domain(oArgs.pvk) > 0:
            for bMKGUID in list(oMKP.keys):
                oMK = oMKP.getMasterKeys(bMKGUID)[0]
                if oMK.decrypted: 
                    if not oMK.get_key() in lstMasterkeys: lstMasterkeys.append(oMK.get_key())
                    if bMKGUID.decode(errors='ignore') == oStateBlob.mkguid: 
                        bMasterkey = oMK.get_key()
                        print(f'[+] Success, user masterkey decrypted: {bMasterkey.hex()}')

    ## Option 3 for getting BME Key: User SID + password (hash)
    if oArgs.mkfile and oArgs.sid and (oArgs.password or oArgs.pwdhash): 
        print('[!] Try MK decryption with user details, might take some time')
        if oArgs.password: oMKP.try_credential(oArgs.sid, oArgs.password)
        else: oMKP.try_credential_hash(oArgs.sid, oArgs.pwdhash)
        for bMKGUID in list(oMKP.keys):
            oMK = oMKP.getMasterKeys(bMKGUID)[0]
            if oMK.decrypted: 
                if not oMK.get_key() in lstMasterkeys: lstMasterkeys.append(oMK.get_key())
                if oABEUserBlob and bMKGUID.decode(errors='ignore') == oABEUserBlob.mkguid:
                    bABEMasterkey = oMK.get_key()
                    print(f'[+] Success, ABE masterkey decrypted: {bABEMasterkey.hex()}')
                if bMKGUID.decode(errors='ignore') == oStateBlob.mkguid: 
                    bMasterkey = oMK.get_key()
                    print(f'[+] Success, Browser masterkey decrypted: {bMasterkey.hex()}')
    if not bBrowserABEKey:
        bBrowserABEKey = tryDPAPIDecrypt(oABEUserBlob, bABEMasterkey)
        if bABEMasterkey not in lstMasterkeys:
            lstMasterkeys.append(bABEMasterkey)
    if bBrowserABEKey:
        if bBrowserABEKey[0:5] == b'\x1f\x00\x00\x00\x02' and "Chrome" in bBrowserABEKey.decode(errors="ignore"):
            # Additional steps for Chrome, see: https://github.com/runassu/chrome_v20_decryption/blob/main/README.md
            if oArgs.verbose: print('[!] Trying to decrypt Chrome ABE key with known static keys')
            aes_key = bytes.fromhex("B31C6E241AC846728DA9C1FAC4936651CFFB944D143AB816276BCC6DA0284787")
            chacha20_key = bytes.fromhex("E98F37D7F4E1FA433D19304DC2258042090E2D1D7EEA7670D41F738D08729660")
            bFlag =bBrowserABEKey[-61:-60]
            bIv = bBrowserABEKey[-60:-48]
            bCiphertext = bBrowserABEKey[-48:-16]
            bTag = bBrowserABEKey[-16:]
            if bFlag == b'\x01':
                if oArgs.verbose: print(f'[!] Using AES-GCM with static key: {aes_key.hex()}')
                cipher = AES.new(aes_key, AES.MODE_GCM, nonce=bIv)
            elif bFlag == b'\x02':
                if oArgs.verbose: print(f'[!] Using ChaCha20-Poly1305 with static key: {chacha20_key.hex()}')
                cipher = ChaCha20_Poly1305.new(key=chacha20_key, nonce=bIv)
            else:
                raise ValueError(f'Unknown flag \'{bFlag}\'')
            try:
                bBrowserABEKey = cipher.decrypt_and_verify(bCiphertext, bTag)
                print(f'[+] Success, decrypted ABE key: {bBrowserABEKey.hex()}')
            except ValueError:
                pass
        else:
            bBrowserABEKey = bBrowserABEKey[-32:]
        print(f'\n[+] Got ABE Master Encryption Key: {bBrowserABEKey.hex()}')
    if not bBrowserBMEKey: 
        bBrowserBMEKey = tryDPAPIDecrypt(oStateBlob, bMasterkey)
        if bMasterkey not in lstMasterkeys: lstMasterkeys.append(bMasterkey)
    if bBrowserBMEKey: print(f'[+] Got Browser Master Encryption Key: {bBrowserBMEKey.hex()}\n')
    else: 
        print('[-] Too bad, no dice, not enough or wrong information')
        exit(0)

    if oArgs.loginfile or oArgs.cookies: print('\n ----- Decrypting logins/cookies -----')
    if bBrowserBMEKey or bBrowserABEKey:
        if lstLogins:
            ## Decrypting logins
            iDecrypted = decryptLogins(lstLogins, bBrowserBMEKey, bBrowserABEKey, lstMasterkeys, oArgs.export, oArgs.verbose)
            print(f'Decrypted {iDecrypted} / {len(lstLogins)} credentials')

        if lstCookies:
            ## Decrypting cookies
            iDecrypted = decryptCookies(lstCookies, bBrowserBMEKey, bBrowserABEKey, lstMasterkeys, oArgs.export, oArgs.verbose)
            print(f'Decrypted {iDecrypted} / {len(lstCookies)} cookies')
    
    if not oArgs.verbose and bBrowserBMEKey: print('[!] To print the results to terminal, rerun with "-v"')
