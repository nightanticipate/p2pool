import sys
import time

from twisted.internet import defer

import p2pool
from p2pool.bitcoin import data as bitcoin_data
from p2pool.util import deferral, jsonrpc, pack
txlookup = {}

@deferral.retry('Error while checking Bitcoin connection:', 1)
@defer.inlineCallbacks
def check(bitcoind, net, args):
    if not (yield net.PARENT.RPC_CHECK(bitcoind)):
        print >>sys.stderr, "    Check failed! Make sure that you're connected to the right bitcoind with --bitcoind-rpc-port, and that it has finished syncing!"
        raise deferral.RetrySilentlyException()
    
    version_check_result = net.VERSION_CHECK((yield bitcoind.rpc_getnetworkinfo())['version'])
    if version_check_result == True: version_check_result = None # deprecated
    if version_check_result == False: version_check_result = 'Coin daemon too old! Upgrade!' # deprecated
    if version_check_result is not None:
        print >>sys.stderr, '    ' + version_check_result
        raise deferral.RetrySilentlyException()
    
    try:
        blockchaininfo = yield bitcoind.rpc_getblockchaininfo()
        try:
            softforks_supported = set(item['id'] for item in blockchaininfo.get('softforks', [])) # not working with 0.19.0.1
        except TypeError:
            softforks_supported = set(item for item in blockchaininfo.get('softforks', [])) # fix for https://github.com/jtoomim/p2pool/issues/38
        try:
            softforks_supported |= set(item['id'] for item in blockchaininfo.get('bip9_softforks', []))
        except TypeError: # https://github.com/bitcoin/bitcoin/pull/7863
            softforks_supported |= set(item for item in blockchaininfo.get('bip9_softforks', []))
    except jsonrpc.Error_for_code(-32601): # Method not found
        softforks_supported = set()
    unsupported_forks = getattr(net, 'SOFTFORKS_REQUIRED', set()) - softforks_supported
    if unsupported_forks:
        print "You are running a coin daemon that does not support all of the "
        print "forking features that have been activated on this blockchain."
        print "Consequently, your node may mine invalid blocks or may mine blocks that"
        print "are not part of the Nakamoto consensus blockchain.\n"
        print "Missing fork features:", ', '.join(unsupported_forks)
        if not args.allow_obsolete_bitcoind:
            print "\nIf you know what you're doing, this error may be overridden by running p2pool"
            print "with the '--allow-obsolete-bitcoind' command-line option.\n\n\n"
            raise deferral.RetrySilentlyException()

@deferral.retry('Error getting work from bitcoind:', 3)
@defer.inlineCallbacks
def getwork(bitcoind, net, use_getblocktemplate=False):
    def go():
        if use_getblocktemplate:
            return bitcoind.rpc_getblocktemplate(dict(mode='template', rules=['segwit','mweb'] if 'mweb' in getattr(net, 'SOFTFORKS_REQUIRED', set()) else ['segwit']))
        else:
            return bitcoind.rpc_getmemorypool()
    try:
        start = time.time()
        work = yield go()
        end = time.time()
    except jsonrpc.Error_for_code(-32601): # Method not found
        use_getblocktemplate = not use_getblocktemplate
        try:
            start = time.time()
            work = yield go()
            end = time.time()
        except jsonrpc.Error_for_code(-32601): # Method not found
            print >>sys.stderr, 'Error: Bitcoin version too old! Upgrade to v0.5 or newer!'
            raise deferral.RetrySilentlyException()

    t0 = time.time()
    if 'height' not in work:
        work['height'] = (yield bitcoind.rpc_getblock(work['previousblockhash']))['height'] + 1
    elif p2pool.DEBUG:
        assert work['height'] == (yield bitcoind.rpc_getblock(work['previousblockhash']))['height'] + 1

    new_work = dict(
        version=work['version'],
        previous_block=int(work['previousblockhash'], 16),
        transactions=work['transactions'],
        transaction_hashes=[bitcoin_data.hex_to_hash(x.get('hash')) if isinstance(x, dict) else None for x in work['transactions']],
        transaction_fees=[x.get('fee', None) if isinstance(x, dict) else None for x in work['transactions']],
        subsidy=work['coinbasevalue'],
        time=work['time'] if 'time' in work else work['curtime'],
        bits=bitcoin_data.FloatingIntegerType().unpack(work['bits'].decode('hex')[::-1]) if isinstance(work['bits'], (str, unicode)) else bitcoin_data.FloatingInteger(work['bits']),
        coinbaseflags=work['coinbaseflags'].decode('hex') if 'coinbaseflags' in work else ''.join(x.decode('hex') for x in work['coinbaseaux'].itervalues()) if 'coinbaseaux' in work else '',
        height=work['height'],
        rules=work.get('rules', []),
        last_update=time.time(),
        use_getblocktemplate=use_getblocktemplate,
        latency=end - start,
        mweb='01' + work['mweb'] if 'mweb' in work else '',
    )
    t1 = time.time()
    if p2pool.BENCH: print "%8.3f ms for helper.py:getwork()." % ((t1 - t0)*1000.)
    defer.returnValue(new_work)

@deferral.retry('Error submitting primary block: (will retry)', 10, 10)
def submit_block_p2p(block, factory, net):
    if factory.conn.value is None:
        print >>sys.stderr, 'No bitcoind connection when block submittal attempted! %s%064x' % (net.PARENT.BLOCK_EXPLORER_URL_PREFIX, bitcoin_data.hash256(bitcoin_data.block_header_type.pack(block['header'])))
        raise deferral.RetrySilentlyException()
    factory.conn.value.send_block(block=block)

@deferral.retry('Error submitting block: (will retry)', 10, 10)
@defer.inlineCallbacks
def submit_block_rpc(block, ignore_failure, bitcoind, bitcoind_work, net):
    segwit_rules = set(['!segwit', 'segwit'])
    segwit_activated = len(segwit_rules - set(bitcoind_work.value['rules'])) < len(segwit_rules)
    # hack: manually serialize blocks with hex transactions instead of using bitcoin/data.py
    hexed_block = bitcoin_data.block_header_type.pack(block['header']).encode('hex') + \
                  pack.VarIntType().pack(len(block['txs'])).encode('hex') + \
                  ''.join(block['txs'])
    if bitcoind_work.value['use_getblocktemplate']:
        try:
            result = yield bitcoind.rpc_submitblock(hexed_block + bitcoind_work.value['mweb'])
        except jsonrpc.Error_for_code(-32601): # Method not found, for older litecoin versions
            result = yield bitcoind.rpc_getblocktemplate(dict(mode='submit', data=hexed_block))
        success = result is None
    else:
        result = yield bitcoind.rpc_getmemorypool(hexed_block)
        success = result
    success_expected = net.PARENT.POW_FUNC(bitcoin_data.block_header_type.pack(block['header'])) <= block['header']['bits'].target
    if (not success and success_expected and not ignore_failure) or (success and not success_expected):
        print >>sys.stderr, 'Block submittal result: %s (%r) Expected: %s' % (success, result, success_expected)

def submit_block(block, ignore_failure, node):
    # fixme: in bitcoin/data.py, block_type needs to be updated to accept hex raw transactions
    # submit_block_p2p(block, node.factory, node.net)
    submit_block_rpc(block, ignore_failure, node.bitcoind, node.bitcoind_work,
                     node.net)

@defer.inlineCallbacks
def check_block_header(bitcoind, block_hash):
    try:
        yield bitcoind.rpc_getblockheader(block_hash)
    except jsonrpc.Error_for_code(-5):
        defer.returnValue(False)
    else:
        defer.returnValue(True)
