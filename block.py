import os
import zlib
import nbt
from io import BytesIO
import sqlite3
from serialize import *
from itemstack import *


class MCMap:
    """A MC map"""
    def __init__(self, path):
        self.world_path = os.path.join(path, "region")
        self.chunk_pos = []
        for ext in ["mca", "mcr"]:
            filenames = [i for i in os.listdir(self.world_path)
                         if i.endswith("."+ext)]
            if len(filenames) > 0:
                self.ext = ext
                break
        for filename in filenames:
            s = filename.split(".")
            cx, cz = int(s[1])*32, int(s[2])*32
            with open(os.path.join(self.world_path, filename), "rb") as f:
                for chkx in range(cx, cx+32):
                    for chkz in range(cz, cz+32):
                        offset = ((chkx%32) + 32*(chkz%32))*4
                        f.seek(offset)
                        if bytesToInt(f.read(3)) != 0:
                            self.chunk_pos.append((chkx, chkz))
        
    def getChunk(self, chkx, chkz):
        return MCChunk(chkx, chkz, self.world_path, self.ext)

    def getBlocksIterator(self):
        for chkx, chkz in self.chunk_pos:
            blocks = self.getChunk(chkx, chkz).blocks
            for block in blocks:
                yield block

class MCChunk:
    """A 16x16 column of nodes"""
    def __init__(self, chkx, chkz, path, ext):
        filename = os.path.join(path,
            "r.{}.{}.{}".format(chkx//32, chkz//32, ext))
        with open(filename, "rb") as f:
            ofs = ((chkx%32) + 32*(chkz%32))*4
            f.seek(ofs)
            offset = bytesToInt(f.read(3)) << 12
            f.seek(offset)
            length = bytesToInt(f.read(4))
            compression_type = bytesToInt(f.read(1))
            data = f.read(length - 1) # 1 byte for compression_type
        if compression_type == 1: # Gzip
            udata = zlib.decompress(data, 15+16)
        elif compression_type == 2: # Zlib
            udata = zlib.decompress(data)
        else:
            raise ValueError("Unsupported compression type")
        raw_data = nbt.read(udata)['']['Level']
        self.blocks = []
        if ext == "mca":
            # Anvil file format
            for section in raw_data["Sections"]:
                self.blocks.append(MCBlock(raw_data, (chkx, chkz), section["Y"], True))
        else:
            for yslice in range(8):
                self.blocks.append(MCBlock(raw_data, (chkx, chkz), yslice, False))
        
        

class MCBlock:
    """A 16x16x16 block"""
    def __init__(self, chunk, chunkpos, yslice, is_anvil=True):
        self.pos = (chunkpos[0], yslice, chunkpos[1])
        if is_anvil:
            # Find the slice
            for section in chunk["Sections"]:
                if section["Y"] == yslice:
                    self.from_section(section)
                    break
        else:
            # No luck, we have to convert
            self.from_chunk(chunk, yslice)
        
        self.tile_entities = []
        for te in chunk["TileEntities"]:
            if (te["y"]>>4) == yslice:
                t = te.copy()
                t["y"] &= 0xf
                self.tile_entities.append(t)

    @staticmethod
    def expand_half_bytes(l):
        l2 = []
        for i in l:
            l2.append(i&0xf)
            l2.append((i>>4)&0xf)
        return l2

    def from_section(self, section):
        self.blocks = section["Blocks"]
        self.data = self.expand_half_bytes(section["Data"])
        self.sky_light = self.expand_half_bytes(section["SkyLight"])
        self.block_light = self.expand_half_bytes(section["BlockLight"])

    @staticmethod
    def extract_slice(data, yslice):
        data2 = [0]*4096
        k = yslice << 4
        k2 = 0
        # Beware: impossible to understand code
        # Sorry, but it has to be as fast as possible,
        # as it is one bottleneck
        # Basically: order is changed from XZY to YZX
        for y in range(16):
            for z in range(16):
                for x in range(16):
                    data2[k2] = data[k]
                    k2 += 1
                    k += 2048
                k = (k&0x7ff)+128
            k = (k&0x7f)+1
        return data2

    @staticmethod
    def extract_slice_half_bytes(data, yslice):
        data2 = [0]*4096
        k = yslice << 3
        k2 = 0
        k3 = 256 # One layer above the previous one
        # Beware: impossible to understand code
        # Even worse than before: that time we've got
        # to extract half bytes at the same time
        # Again, order is changed from XZY to YZX
        for y in range(0, 16, 2): # 2 values for y at a time
            for z in range(16):
                for x in range(16):
                    data2[k2] = data[k]&0xf
                    data2[k3] = (data[k]>>4)&0xf
                    k2 += 1
                    k3 += 1
                    k += 1024
                k = (k&0x3ff)+64
            k = (k&0x3f)+1
            k2 += 256 # Skip a layer
            k3 += 256
        return data2
    
    def from_chunk(self, chunk, yslice):
        self.blocks = self.extract_slice(chunk["Blocks"], yslice)
        self.data = self.extract_slice_half_bytes(chunk["Data"], yslice)
        self.sky_light = self.extract_slice_half_bytes(chunk["SkyLight"], yslice)
        self.block_light = self.extract_slice_half_bytes(chunk["BlockLight"], yslice)

class MTBlock:
    def __init__(self, name_id_mapping):
        self.name_id_mapping = name_id_mapping
        self.content = [0]*4096
        self.param1 = [0]*4096
        self.param2 = [0]*4096
        self.metadata = {}
        self.pos = (0, 0, 0)

    def fromMCBlock(self, mcblock, conversion_table):
        self.pos = (mcblock.pos[0], mcblock.pos[1]-4, mcblock.pos[2])
        content = self.content
        param1 = self.param1
        param2 = self.param2
        blocks = mcblock.blocks
        data = mcblock.data
        skylight = mcblock.sky_light
        blocklight = mcblock.block_light
        
        for i in range(4096):
            content[i], param2[i] = conversion_table[blocks[i]][data[i]]
            param1[i] = max(blocklight[i], skylight[i])|(blocklight[i]<<4)

        for te in mcblock.tile_entities:
            id = te["id"]
            x, y, z = te["x"], te["y"], te["z"]
            index = ((y&0xf)<<8)|((z&0xf)<<4)|(x&0xf)
            f = te_convert.get(id.lower(), lambda arg: (None, None, None)) # Do nothing if not found
            block, p2, meta = f(te)
            if block != None:
                blocks[index] = block
            if p2 != None:
                param2[index] = p2
            if meta != None:
                self.metadata[(x&0xf, y&0xf, z&0xf)] = meta

    def save(self):
        os = BytesIO()
        writeU8(os, 25) # Version
        flags = 0x00
        if self.pos[1] < -1:
            flags |= 0x01
        writeU8(os, flags)
        writeU8(os, 2) # content_width
        writeU8(os, 2) # params_width

        cbuffer = BytesIO()
        # Bulk node data
        content = self.content
        k = 0
        nimap = {}
        rev_nimap = []
        first_free_content = 0
        for z in range(16):
            for y in range(16):
                for x in range(16):
                    #writeU16(cbuffer, content[k])
                    c = content[k]
                    if c in nimap:
                        writeU16(cbuffer, nimap[c])
                    else:
                        nimap[c] = first_free_content
                        writeU16(cbuffer, first_free_content)
                        rev_nimap.append(c)
                        first_free_content += 1
                    k += 1
                k += (256-16)
            k += (16-16*256)
        param1 = self.param1
        k = 0
        for z in range(16):
            for y in range(16):
                for x in range(16):
                    writeU8(cbuffer, param1[k])
                    k += 1
                k += (256-16)
            k += (16-16*256)
        param2 = self.param2
        k = 0
        for z in range(16):
            for y in range(16):
                for x in range(16):
                    writeU8(cbuffer, param2[k])
                    k += 1
                k += (256-16)
            k += (16-16*256)
        os.write(zlib.compress(cbuffer.getvalue()))

        # Nodemeta
        meta = self.metadata
        cbuffer = BytesIO()
        writeU8(cbuffer, 1) # Version
        writeU16(cbuffer, len(meta))
        for pos, data in meta.items():
            writeU16(cbuffer, (pos[2]<<16)|(pos[1]<<8)|pos[0])
            writeU32(cbuffer, len(data[0]))
            for name, val in data[0].items():
                writeString(cbuffer, name)
                writeLongString(cbuffer, str(val))
            serialize_inv(cbuffer, data[1])
        os.write(zlib.compress(cbuffer.getvalue()))

        # Static objects
        writeU8(os, 0) # Version
        writeU16(os, 0) # Number of objects

        # Timestamp
        writeU32(os, 0xffffffff) # BLOCK_TIMESTAMP_UNDEFINED

        # Name-ID mapping
        writeU8(os, 0) # Version
        writeU16(os, len(rev_nimap))
        for i in range(len(rev_nimap)):
            writeU16(os, i)
            writeString(os, self.name_id_mapping[rev_nimap[i]])

        # Node timer
        writeU8(os, 2+4+4) # Timer data len
        writeU16(os, 0) # Number of timers
        
        return os.getvalue()

class MTMap:
    def __init__(self, path):
        self.world_path = path
        self.blocks = []

    @staticmethod
    def getBlockAsInteger(p):
        return p[0]+4096*(p[1]+4096*p[2])

    @staticmethod
    def fromMCMapBlocksIterator(mcmap, name_id_mapping, conversion_table):
        for mcblock in mcmap.getBlocksIterator():
            mtblock = MTBlock(name_id_mapping)
            mtblock.fromMCBlock(mcblock, conversion_table)
            yield mtblock

    def fromMCMap(self, mcmap, nimap, ct):
        self.blocks = self.fromMCMapBlocksIterator(mcmap, nimap, ct)

    def save(self):
        conn = sqlite3.connect(os.path.join(self.world_path, "map.sqlite"))
        cur = conn.cursor()

        cur.execute("CREATE TABLE IF NOT EXISTS `blocks` (\
            `pos` INT NOT NULL PRIMARY KEY, `data` BLOB);")

        num_saved = 0
        for block in self.blocks:
            if num_saved%50 == 0:
                print("Saved", num_saved, "blocks")
                conn.commit()
            num_saved += 1
            cur.execute("INSERT INTO blocks VALUES (?,?)",
                        (self.getBlockAsInteger(block.pos),
                        block.save()))

        conn.commit()
        conn.close()

    
if __name__ == "__main__":
    # Tests
    from random import randrange
    t = [randrange(256) for i in range(2048*8)]
    assert(MCBlock.extract_slice(MCBlock.expand_half_bytes(t), 0)
          == MCBlock.extract_slice_half_bytes(t, 0))
    
    from time import time
    t0 = time()
    s1 = MCBlock.extract_slice(MCBlock.expand_half_bytes(t), 1)
    print(time()-t0)
    t0 = time()
    s2 = MCBlock.extract_slice_half_bytes(t, 1)
    print(time()-t0)
    assert(s1 == s2)