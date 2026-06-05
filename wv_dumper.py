#!/usr/bin/env python3
"""Custom Widevine L3 CDM dumper for Frida 17.x."""

import time
import logging
import json
import os
from Crypto.PublicKey import RSA

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
log = logging.getLogger(__name__)

CDM_VERSION = '17.0.0'
KNOWN_PK_FUNCS = ['rnmsglvj','polorucp','kqzqahjq','pldrclfq','kgaitijd','dnvffnze','cwkfcplc','crhqcdet','igrqajte','ofskesua','ppsniaij','qkfrcjtw']
WIDEVINE_LIBS = ['liboemcrypto.so','libwvhidl.so','libwvaidl.so','libwvdrmengine.so','libdrmwvmplugin.so','libwvm.so']

FRIDA_SCRIPT = r"""
var CDM_VER = '%s';
var KNOWN = %s;
var pkFound = false, cidFound = false;
var savedPk = null, savedCid = null;

function TE() {}
TE.prototype.encode = function(s) {
    var o = [];
    for (var i = 0; i < s.length; i++) {
        var c = s.codePointAt(i);
        if (c <= 0x7F) { o.push(0); }
        else if (c <= 0x7FF) { o.push(0xC0); c -= 0x80; }
        else if (c <= 0xFFFF) { o.push(0xE0); c -= 0x1000; }
        else { o.push(0xF0); c -= 0x10000; }
        var b = 0;
        while (c >= 0x40) { o.push(0x80 | (c & 0x3F)); c >>= 6; b++; }
        o.push(c);
    }
    return o;
};
function a2bs(b) { var s=''; for(var i=0;i<b.length;i++) s+=String.fromCharCode(b[i]); return s; }
function getKeyLen(k) {
    var p=1, b=k.charCodeAt(p++), l=b&0x7F; b=0;
    for(var i=0;i<l;++i) b=(b*256)+k.charCodeAt(p++);
    return p+Math.abs(b);
}

function hookPK(addr) {
    Interceptor.attach(ptr(addr), {
        onEnter: function(a) {
            if (!a[6].isNull()) {
                var sz = a[6].toInt32();
                if (sz >= 1000 && sz <= 2000 && !a[5].isNull()) {
                    var buf = a[5].readByteArray(sz);
                    var bytes = new Uint8Array(buf);
                    if (bytes[0]===0x30 && bytes[1]===0x82) {
                        try {
                            var bs = a2bs(bytes);
                            var kl = getKeyLen(bs);
                            var key = bytes.slice(0, kl);
                            savedPk = key;
                            pkFound = true;
                            send({type:'pk', data: key});
                        } catch(e) { console.log(e); }
                    }
                }
            }
        }
    });
}

function disablePM(addr) {
    Interceptor.attach(addr, { onLeave: function(r) { r.replace(ptr(0)); }});
}

function hookPKR(addr) {
    Interceptor.attach(ptr(addr), {
        onEnter: function(a) { this.ret = (CDM_VER==='16.1.0'||CDM_VER==='17.0.0')?a[5]:a[4]; },
        onLeave: function() {
            if (this.ret) {
                var sz = Memory.readU32(ptr(this.ret).add(Process.pointerSize));
                var arr = Memory.readByteArray(this.ret.add(Process.pointerSize*2).readPointer(), sz);
                savedCid = arr;
                cidFound = true;
                send({type:'cid', data: arr});
            }
        }
    });
}

function hookLib(name) {
    var lib = Process.findModuleByName(name);
    if (!lib) { send({type:'info',msg:'Not found: '+name}); return; }
    send({type:'info',msg:'Hooking '+name+' @ '+lib.base});
    Module.enumerateExportsSync(name).forEach(function(e) {
        try {
            if (e.name.indexOf('UsePrivacyMode')!==-1) { disablePM(ptr(e.address)); send({type:'info',msg:'PM: '+e.name}); }
            else if (e.name.indexOf('PrepareKeyRequest')!==-1) { hookPKR(ptr(e.address)); send({type:'info',msg:'PKR: '+e.name}); }
            else if (KNOWN.indexOf(e.name)!==-1 && e.type==='function') { hookPK(ptr(e.address)); send({type:'info',msg:'PK: '+e.name}); }
            else if (e.name.match(/^[a-z]+$/) && e.type==='function' && !pkFound) { hookPK(ptr(e.address)); }
        } catch(ex) { console.log('Err: '+ex+' @ '+e.name); }
    });
}

rpc.exports = {
    hooklibs: function(arr) { arr.forEach(function(n){hookLib(n);}); },
    status: function() { return {pk:pkFound,cid:cidFound}; },
    getkeys: function() { return {pk: savedPk, cid: savedCid}; }
};
""" % (CDM_VERSION, json.dumps(KNOWN_PK_FUNCS))

def main():
    import frida

    device = frida.get_usb_device()
    log.info('Connected to %s', device.name)

    wv_pid = None
    for p in device.enumerate_processes():
        if 'widevine' in p.name.lower():
            wv_pid = p.pid
            log.info('Widevine process: %s (PID %d)', p.name, wv_pid)
            break
    if not wv_pid:
        log.error('No Widevine process found')
        return

    session = device.attach(wv_pid)
    script = session.create_script(FRIDA_SCRIPT)

    def on_msg(msg, data):
        t = msg.get('type','')
        if t == 'info':
            log.info(msg.get('msg',''))
        elif t == 'pk':
            log.info('PRIVATE KEY DUMPED')
        elif t == 'cid':
            log.info('CLIENT ID DUMPED')

    script.on('message', on_msg)
    script.load()

    log.info('Hooking libraries...')
    script.exports_sync.hooklibs(WIDEVINE_LIBS)

    log.info('Now play DRM content on your phone!')
    log.info('e.g. open https://bitmovin.com/demos/drm in a browser, or play any Netflix/Tidal video')
    log.info('Waiting for keys...')

    for _ in range(300):
        time.sleep(1)
        s = script.exports_sync.status()
        if s.get('pk') and s.get('cid'):
            log.info('Keys dumped!')
            break
    else:
        log.error('Timed out. Make sure DRM content is playing.')
        session.detach()
        return

    keys = script.exports_sync.getkeys()
    pk_bytes = keys['pk']
    cid_bytes = keys['cid']

    pk_key = RSA.import_key(pk_bytes)
    log.info('Private key loaded: %d-bit', pk_key.size_in_bits())

    outdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cdm')
    os.makedirs(outdir, exist_ok=True)

    with open(os.path.join(outdir, 'device_private_key.pem'), 'wb') as f:
        f.write(pk_key.exportKey('PEM'))
    log.info('Saved device_private_key.pem')

    with open(os.path.join(outdir, 'device_client_id_blob'), 'wb') as f:
        f.write(cid_bytes)
    log.info('Saved device_client_id_blob')

    try:
        from pywidevine.device import Device
        wvd = os.path.join(outdir, 'widevine.wvd')
        dev = Device.create_l3(cert=cid_bytes, private_key=pk_key, security_level=3, name=device.name)
        dev.save(wvd)
        log.info('Saved widevine.wvd')
    except Exception as e:
        log.error('Failed to create .wvd: %s', e)

    session.detach()

if __name__ == '__main__':
    main()
