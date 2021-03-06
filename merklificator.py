import argparse
import collections
import gzip
import json
import logging
import math
import os
import statistics
import time
from typing import Dict, List, TypedDict

from roaringbitmap import RoaringBitmap
from sparklines import sparklines
from tdigest import RawTDigest
from engineering_notation import EngNumber

logger = logging.getLogger("")
MAXCODESIZE = 0x6000 # from EIP 170

def represent_contract(bytemap, codesize, chunkmap, chunk_size):
    contract_representation = ""
    for b in range(0, codesize):
        if b % chunk_size == 0:
            contract_representation += "|"
        b_in_chunkmap = (b // chunk_size) in chunkmap
        char = "."      # this is uninteresting: contract byte that wasn't executed nor chunked
        if b in bytemap:
            char = 'X'  # This is bad: executed bytecode that didn't get merklized
        if b_in_chunkmap:
            char = 'm'  # this is overhead: merklized but unexecuted code
        if b in bytemap and b_in_chunkmap:
            char = "M"  # this is OK: executed and merklized code
        contract_representation += char
    print(contract_representation,"\n")

def clamp(n : int, min_n : int, max_n : int) -> int:
    if n >= max_n:
        return max_n
    if n <= min_n:
        return min_n
    return n

def sparkline_sizes(sorted_sizes : List) -> str :
    # sizes is sorted so we can take advantage of that to accelerate things
    median = int(statistics.median(sorted_sizes))
    bucket_size = 1
    top_bucket = 2 * median # up to median there's half of the items. Since that's typically a short range anyway, let's show twice that.
    buckets_maxcontent = range(1, top_bucket, bucket_size)  # each bucket contains values up to this, inclusive
    if len(buckets_maxcontent)==0:
        #logger.info(f"Can't bucketize, moving on. sizes={sorted_sizes}, median={median}, block={block}")
        return f"CAN'T BUCKETIZE! sizes={sorted_sizes}"
    buckets_contents = [0 for b in buckets_maxcontent]
    maxbucket = len(buckets_maxcontent)
    count = 0
    for s in sorted_sizes:
        b = math.ceil(s / bucket_size)
        if b >= maxbucket:
            # since the sizes are sorted, at this point we know we're finished
            break
        count += 1
        b = clamp(b, 0, len(buckets_contents) - 1)
        buckets_contents[b] += 1

    sl = sparklines(buckets_contents)[0]
    remaining = (1 - count / len(sorted_sizes)) * 100
    line = f"median={median}\t\t{buckets_maxcontent[0]}{sl}{buckets_maxcontent[-1]} (+{remaining:.0f}% more)"
    return line

# calculate the number of hashes needed to merklize the given bitmap of chunks
def merklize(chunkmap : RoaringBitmap, arity : int, max_theoretical_chunks : int, filename : str = None) -> int:
    # hashes at tree level 0 = as many as chunks
    # max hashes at tree level N = (num of level N-1) / arity
    # we assume fixed number of levels = log (max chunks) / log(arity)
    # Leaves    0,1,2,3,4,5
    # L0        0   1   2
    # L1        0       1
    # L2        0
    if filename is not None:
        dotfile = open(filename,"w")
        dotfile.write('graph  {\n')
    num_levels = math.log(max_theoretical_chunks) / math.log(arity)
    assert(chunkmap.max() < max_theoretical_chunks)
    current_level = 0
    num_hashes = 0
    # theoretical means assuming that all the chunks are there
    theoretical_siblings = max_theoretical_chunks
    map = chunkmap
    while theoretical_siblings >= arity:
        # `map` contains the tree nodes at level `current_level`-1
        # from each n-ary set of siblings we check whether to add a parent node on the level `current_level`, into the variable `parents`

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("-----------")
            logger.debug(f"L{current_level-1} pot_nodes={theoretical_siblings} actual_nodes={len(map)}")
        parents = []
        max_actual_parents = math.ceil((map.max() + 1) / arity) # optimization to not look for overlaps when the map is known to be empty
                                                                # +1 because map.max() is an index, not the length
        theoretical_parents = math.ceil(theoretical_siblings / arity)
        hashes_missing = 0

        parents_to_check = max_actual_parents
        if filename is not None:
            parents_to_check = theoretical_parents # visit all the theoretical nodes even if not currently present, so we can draw them
        # for each possible parent, check if its siblings are present in the map
        for p in range(0, parents_to_check):
            siblings_start = p * arity
            siblings_end = (p + 1) * arity
            siblings_end = min(siblings_end, theoretical_siblings)
            overlap = map.clamp(siblings_start, siblings_end)
            lo = len(overlap)

            # generate visualization
            if filename is not None:
                parent = f"L{current_level+1}_{p}"
                for s in range(siblings_start, siblings_end):
                    sibling = f"L{current_level}_{s}"
                    dotfile.write(parent + " -- " + sibling + "\n")
                    if lo == 0:
                        # no sibling is present, so don't fill the bubbles
                        continue
                    if not s in map:
                        dotfile.write(sibling + ' [style = "filled" fillcolor = red] \n') # missing hash that needs to be provided
                    else:
                        if current_level == 0:
                            dotfile.write(sibling + ' [style = "filled" fillcolor = lawngreen] \n') # a leaf that is present
                        else:
                            dotfile.write(sibling + ' [style = "filled" fillcolor = gray70] \n') # a node that is present (calculated by receiver)

            if lo == 0:
                # these siblings are absent in the tree, hence their parent is also absent
                continue
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(f"L{current_level} checking siblings {range(siblings_start,siblings_end)} with parent {p}. Max parent expected = {max_actual_parents}")
            assert(p < max_actual_parents) # sanity check: there's a sibling present, so its parent should be in the expected range

            # at least one of these siblings is present, so the parent must be present too
            parents.append(p)
            hashes_missing = arity - lo
            num_hashes += hashes_missing

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"L{current_level}: parents={parents}, num_missing_hashes={hashes_missing}")

        # switch map to parents'
        map = RoaringBitmap(parents)
        theoretical_siblings = theoretical_parents
        current_level += 1
    root = f"L{current_level}_{p}"
    if filename is not None:
        dotfile.write(root + ' [style = "filled" fillcolor = gray70] \n')  #fix the root's color
        dotfile.write('}')
        dotfile.close()
    return num_hashes

def chunkmap_from_bytemap(bytemap: RoaringBitmap, chunk_size: int) -> RoaringBitmap:
    max_contract_chunk = bytemap.max() // chunk_size
    if chunk_size == 1:
        # special case for speed
        chunkmap = bytemap
    else:
        chunkmap = RoaringBitmap()
        for c in range(0, max_contract_chunk + 1):
            chunk_start = c * chunk_size
            chunk_stop = (c + 1) * chunk_size
            # chunkrange = range(chunk_start, chunk_stop)
            # z1 = len(bytemap.clamp(chunk_start, chunk_stop)) == 0 #fastest
            # z2 = bytemap.intersection_len(chunkrange) == 0
            # z3 = bytemap.isdisjoint(chunkrange)
            # assert(z1 == z2 and z2 == z3)
            # chunkificator = chunkificators[c]
            # if not bytemap.isdisjoint(chunkificator):
            overlap = bytemap.clamp(chunk_start, chunk_stop)
            if len(overlap) != 0:
                chunkmap.add(c)
    return chunkmap

def main():
    parser = argparse.ArgumentParser(
        description='Reads a directory or multiple files containing segments from transactions, and applies fixed chunking to them to calculate the resulting witness sizes',
        epilog='Note that hash_sizes have no effect on the tree calculations; they are just used to get the bytes consumed by hashes.',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("traces_dir", help="Directory with, or multiple space-separated trace files in .json.gz format", nargs='+')
    parser.add_argument("-s", "--chunk_size", help="Chunk size in bytes. Can be multiple space-separated values.", type=int, default=[32], nargs='*')
    parser.add_argument("-m", "--hash_size", help="Hash size in bytes for construction of the Merkle tree. Can be multiple space-separated values.\n", type=int, default=[32], nargs='*')
    parser.add_argument("-a", "--arity", help="Number of children per node of the Merkle tree", type=int, default=2)
    parser.add_argument("-l", "--log", help="Log level", type=str, default="INFO")
    parser.add_argument("-j", "--job_ID", help="ID to distinguish in parallel runs", type=int, default=None)
    parser.add_argument("-d", "--detail_level", help="3=transaction, 2=contract, 1=block, 0=file. One level implies the lower ones.", type=int, default=1)
    parser.add_argument("-g", "--segment_stats", help="Whether to calculate and show segments stats", default=False, action='store_true')

    args = parser.parse_args()

    loglevel_num = getattr(logging, args.log.upper(), None)
    if not isinstance(loglevel_num, int):
        raise ValueError(f"Invalid log level: {args.loglevel}")
    logging.basicConfig(level=loglevel_num, format= \
        ("" if args.job_ID is None else f'{args.job_ID:4} | ') + '%(asctime)s %(message)s', datefmt='%H:%M:%S')
    logger = logging.getLogger("")

    # help the IDE's autocomplete and typing
    chunk_sizes : List[int] = args.chunk_size
    hash_sizes : List[int] = args.hash_size
    traces_dir : List[str] = args.traces_dir
    arity : int = args.arity
    detail_level : int = args.detail_level
    segment_stats : bool = args.segment_stats

    del args

    contract_data = TypedDict('contract_data', {'instances':int, 'size':int, 'map':RoaringBitmap}, total=True)

    Chunkification_record = collections.namedtuple('Chunkification_record', [])

    segment_sizes : List[int] = []

    if len(traces_dir) == 1 and os.path.isdir(traces_dir[0]):
        files = sorted([os.path.join(traces_dir[0], f) for f in os.listdir(traces_dir[0]) if (".json.gz" == f[-8:])])
    else:
        files = sorted(traces_dir)
    total_executed_bytes = 0
    total_hashes: List[int] = [0 for x in chunk_sizes]  # one element per chunk size
    total_chunks: List[int] = [0 for x in chunk_sizes]  # one element per chunk size
    total_segsize_digest = RawTDigest()
    total_blocks = 0
    block : int
    print(f"Chunking for tree arity={arity}, chunk size={chunk_sizes}, hash size={hash_sizes}")
    for f in files:
        t0 = time.time()
        blocks: int = 0
        with gzip.open(f, 'rb') as gzf:
            block_traces = json.load(gzf)
        file_executed_bytes = 0
        file_hashes: List[int] = [0 for x in chunk_sizes]  # one element per chunk size
        file_chunks: List[int] = [0 for x in chunk_sizes]  # one element per chunk size
        file_segsizes : List[int] = []
        file_segsize_digest = RawTDigest()
        for block in block_traces:
            traces = block_traces[block]
            blocks += 1
            total_blocks +=1
            if len(traces)==0:
                logger.debug(f"Block {block} is empty")
                continue
            dict_contracts : Dict[str, contract_data] = {}
            reused_contracts = 0
            empty_transactions = 0
            num_bytes_code = 0
            num_bytes_chunks = 0
            block_segsizes : List[int] = []
            block_numsegments = 0
            for t in traces:
                tx_hash: str = t["Tx"]
                if tx_hash is None:
                    # this was a transaction without opcodes - a value transfer
                    empty_transactions += 1
                    continue
                logger.debug(f"Tx {t['TxAddr']} has {len(t['Segments'])} segments")
                codehash : str = t["CodeHash"]
                data = dict_contracts.get(codehash)
                if data is None:
                    bytemap = RoaringBitmap()
                    instances = 1
                    size = t['CodeSize']
                else:
                    reused_contracts += 1
                    bytemap = data['map']
                    instances = data['instances']+1
                    size = data['size']

                tx_segsizes: List[int] = []
                for s in t["Segments"]:
                    start = s["Start"]
                    end = s["End"]
                    range_code = range(start, end+1)
                    bytemap.update(range_code)
                    if segment_stats:
                        length = end - start + 1
                        tx_segsizes.append(length)
                block_numsegments += len(t['Segments'])
                dict_contracts[codehash] = contract_data(instances=instances, size=size, map=bytemap)
                #del t["Segments"]

                # transaction-level segment stats
                if segment_stats:
                    tx_segsizes = sorted(tx_segsizes)
                    block_segsizes += tx_segsizes
                if detail_level >= 3:
                    segstats = f"seg_sizes:{sparkline_sizes(tx_segsizes)}" if segment_stats else ""
                    print(f"Block {block} "
                          f"codehash={codehash} "
                          f"tx={t['TxAddr']} "
                          f"segs={len(tx_segsizes)} "
                          + segstats
                          )

            # coherency check: did we classify all the transactions?
            executed_transactions = sum([c["instances"] for c in dict_contracts.values()])
            assert(len(traces) == executed_transactions + empty_transactions)

            if executed_transactions == 0:
                # There were no transactions with segments, so nothing to calculate for this block
                logger.debug(f"Block {block} had no segments")
                continue

            block_executed_bytes : int = 0
            block_hashes : List[int] = [0 for x in chunk_sizes] # one element per chunk size
            block_chunks : List[int] = [0 for x in chunk_sizes] # one element per chunk size

            # chunkification of the contracts executed in the block
            for codehash, data in dict_contracts.items():
                instances = data['instances']
                codesize = data['size']
                bytemap = data['map']
                executed_bytes = len(bytemap)
                block_executed_bytes += executed_bytes
                file_executed_bytes += executed_bytes
                total_executed_bytes += executed_bytes
                for csi in range(0, len(chunk_sizes)):
                    chunk_size = chunk_sizes[csi]
                    #maxcode_chunk = MAXCODESIZE // chunk_size
                    chunkmap = chunkmap_from_bytemap(bytemap, chunk_size)
                    max_theoretical_chunks = MAXCODESIZE // chunk_size
                    merklization_hashes = merklize(chunkmap, arity, max_theoretical_chunks)

                    num_chunks = len(chunkmap)
                    chunked_bytes = num_chunks * chunk_size

                    # make it easier to see outliers
                    highlighter : str = ""
                    if chunked_bytes < executed_bytes: # sanity check
                        logger.error(f"Contract {codehash} in block {block} executes {executed_bytes} but merklizes to {chunked_bytes}")
                        highlighter = "\t\t" + "??????"
                        represent_contract(bytemap, codesize, chunkmap, chunk_size)

                    if detail_level >=2:
                        chunk_waste = ((chunked_bytes - executed_bytes) / chunked_bytes) * 100
                        print(f"Contract {codehash}: "
                              f"{instances} txs "
                              f"size={codesize}\t"
                              f"executed={executed_bytes}\t"
                              f"chunksize={chunk_size}\t"
                              f"chunks={num_chunks}={chunked_bytes}B\t"
                              f"wasted={chunk_waste:.0f}%\t"
                              f"hashes={merklization_hashes}"
                              +highlighter
                        )

                    block_chunks[csi] += num_chunks
                    block_hashes[csi] += merklization_hashes
                    file_chunks[csi] += num_chunks
                    file_hashes[csi] += merklization_hashes
                    total_chunks[csi] += num_chunks
                    total_hashes[csi] += merklization_hashes

            # block-level merklization stats
            if detail_level >=1:
                print(f"Block {block}: "
                    f"exec={block_executed_bytes / 1024:.1f}K\t"
                    , end=''
                )
                if segment_stats:
                    # sorting helps other steps be faster (e.g., stats.median)
                    block_segsizes = sorted(block_segsizes)
                    print(
                        f"segs={len(block_segsizes)}\t"
                        f"seg_sizes:{sparkline_sizes(block_segsizes)}")
                    file_segsizes += block_segsizes
                else:
                    print()

                for csi in range(len(chunk_sizes)):  # we need the index
                    chunk_size = chunk_sizes[csi]
                    chunks = block_chunks[csi]
                    hashes = block_hashes[csi]
                    block_chunk_bytes = chunks * chunk_size
                    block_merklization_chunk_overhead = (block_chunk_bytes - block_executed_bytes) / block_executed_bytes * 100
                    print(
                        f"\t"
                        f"chunksize={chunk_size:2}\t"
                        f"chunk_oh={block_merklization_chunk_overhead :5.1f}% ({EngNumber(chunks)} chunks) + {EngNumber(hashes)} hashes\t"
                        # ,end=''
                    )
                    for hash_size in hash_sizes:
                        block_hash_bytes = hashes * hash_size
                        block_merklization_hash_overhead = block_hash_bytes / block_executed_bytes * 100
                        block_merklization_bytes = block_chunk_bytes + block_hash_bytes
                        block_merklization_overhead = (block_merklization_bytes - block_executed_bytes) / block_executed_bytes * 100
                        print(
                            f"\t\t"
                            f"hashsize={hash_size:2}"
                            f"\t hash_oh={block_merklization_hash_overhead :5.1f}%"
                            f"\t\ttotal_oh={block_merklization_overhead :5.1f}%"
                            # f"mkztn={block_merklization_bytes / 1024:.1f} K "
                            # f"= {block_chunk_bytes / 1024:.1f} K ({EngNumber(chunks)} chunks) + {block_hash_bytes / 1024:.1f} K ({EngNumber(hashes)} hashes)"
                        )
        del block_traces  # help the garbage collector?


        # file-level merklization stats
        print(
            f"file {f}: "
            f"blocks={blocks}\t"
            f"exec={file_executed_bytes / 1024:.1f}K\t"
            , end=''
        )
        if segment_stats:
            # sorting helps other steps be faster (e.g., stats.median)
            file_segsizes = sorted(file_segsizes)
            print(
                f"segs={len(file_segsizes)}\t"
                f"seg_sizes:{sparkline_sizes(file_segsizes)}")
        else:
            print()

        for csi in range(len(chunk_sizes)): # we need the index
            chunk_size = chunk_sizes[csi]
            chunks = file_chunks[csi]
            hashes = file_hashes[csi]
            file_chunk_bytes = chunks * chunk_size
            file_merklization_chunk_overhead = (file_chunk_bytes - file_executed_bytes) / file_executed_bytes * 100
            print(
                f"\t"
                f"chunksize={chunk_size:2}\t"
                f"chunk_oh={file_merklization_chunk_overhead :5.1f}% ({EngNumber(chunks)} chunks) + {EngNumber(hashes)} hashes\t"
                #,end=''
            )
            for hash_size in hash_sizes:
                file_hash_bytes = hashes * hash_size
                file_merklization_hash_overhead = file_hash_bytes / file_executed_bytes * 100
                file_merklization_bytes = file_chunk_bytes + file_hash_bytes
                file_merklization_overhead = (file_merklization_bytes - file_executed_bytes) / file_executed_bytes * 100
                print(
                    f"\t\t"
                    f"hashsize={hash_size:2}"
                    f"\t hash_oh={file_merklization_hash_overhead :5.1f}%"
                    f"\t\ttotal_oh={file_merklization_overhead :5.1f}%"
                    #f"mkztn={file_merklization_bytes / 1024:.1f} K "
                    #f"= {file_chunk_bytes / 1024:.1f} K ({EngNumber(chunks)} chunks) + {file_hash_bytes / 1024:.1f} K ({EngNumber(hashes)} hashes)"
                )
        # log speed
        t_file = time.time() - t0
        logger.info(f"file {f}: "
                     f"{blocks} blocks in {t_file:.0f} seconds = {blocks/t_file:.1f}bps.")

        # global running stats
        if len(files) < 2:
            continue
        print(
            f"running total: "
            f"blocks={total_blocks}\t"
            ,end=''
        )
        if segment_stats:
            for s in file_segsizes:
                total_segsize_digest.insert(s)
            print(f"\testimated median segsize:{total_segsize_digest.quantile(0.5):.1f}")
        else:
            print()

        for csi in range(len(chunk_sizes)): # we need the index
            chunk_size = chunk_sizes[csi]
            chunks = total_chunks[csi]
            hashes = total_hashes[csi]
            total_chunk_bytes = chunks * chunk_size
            total_merklization_chunk_overhead = (total_chunk_bytes - total_executed_bytes) / total_executed_bytes * 100
            print(
                f"\t"
                f"chunksize={chunk_size:2}\t"
                f"chunk_oh={total_merklization_chunk_overhead :5.1f}% ({EngNumber(chunks)} chunks) + {EngNumber(hashes)} hashes\t"
                #,end=''
            )
            for hash_size in hash_sizes:
                total_hash_bytes = hashes * hash_size
                total_merklization_hash_overhead = total_hash_bytes / total_executed_bytes * 100
                total_merklization_bytes = total_chunk_bytes + total_hash_bytes
                total_merklization_overhead = (total_merklization_bytes - total_executed_bytes) / total_executed_bytes * 100
                print(
                    f"\t\t"
                    f"hashsize={hash_size:2}"
                    f"\t hash_oh={total_merklization_hash_overhead :5.1f}% ({EngNumber(hashes)} hashes)"
                    f"\t\ttotal_oh={total_merklization_overhead :5.1f}%\t"
                    #f"mkztn={total_merklization_bytes / 1024:.1f} K "
                    #f"= {total_chunk_bytes / 1024:.1f} K ({EngNumber(chunks)} chunks) + {total_hash_bytes / 1024:.1f} K ({EngNumber(hashes)} hashes)"
                )

if __name__ == "__main__":
    main()
