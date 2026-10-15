"""Repack this TP rank's rows without changing weight or scale bytes."""
import hashlib,json,mmap,os,pathlib,struct,sys
import numpy as np
root,out,rank=pathlib.Path(sys.argv[1]),pathlib.Path(sys.argv[2]),int(sys.argv[3])
out.mkdir(parents=True,exist_ok=True)
index=json.loads((root/'model.safetensors.index.json').read_text())['weight_map']
records=[]
for key,filename in sorted(index.items()):
 if not key.endswith('.engram.embed.weight'):continue
 layer=int(key.split('.')[1]);source=root/filename;scale_key=key[:-6]+'scale'
 assert index[scale_key]==filename
 with source.open('rb') as stream:
  length=struct.unpack('<Q',stream.read(8))[0];header=json.loads(stream.read(length));weight,scale=header[key],header[scale_key]
  rows=weight['shape'][0];assert weight['shape']==[rows,256] and scale['shape']==[rows,8]
  lo,hi=rows*rank//4,rows*(rank+1)//4
  expected=4096+(hi-lo)*264;destination=out/f'engram-l{layer}-r{rank}of4.bin'
  packed_header=struct.pack('<6Q',0x31344e4531565344,layer,lo,hi,rows,264).ljust(4096,b'\0')
  if destination.exists():
   assert destination.stat().st_size==expected and destination.open('rb').read(4096)==packed_header
   records.append({'layer':layer,'bytes':expected,'reused':True});continue
  temporary=destination.with_suffix('.partial')
  with mmap.mmap(stream.fileno(),0,access=mmap.ACCESS_READ) as mapping,temporary.open('wb') as target:
   weights=np.ndarray((rows,256),dtype=np.uint8,buffer=mapping,offset=8+length+weight['data_offsets'][0])
   scales=np.ndarray((rows,8),dtype=np.uint8,buffer=mapping,offset=8+length+scale['data_offsets'][0])
   target.write(packed_header)
   for start in range(lo,hi,65536):
    end=min(start+65536,hi);packed=np.empty((end-start,264),dtype=np.uint8)
    packed[:,:256]=weights[start:end];packed[:,256:]=scales[start:end];target.write(packed.tobytes())
   target.flush();os.fsync(target.fileno());del weights,scales
  assert temporary.stat().st_size==expected
  os.replace(temporary,destination)
  if hasattr(os,'posix_fadvise'):os.posix_fadvise(stream.fileno(),0,0,os.POSIX_FADV_DONTNEED)
 samples=hashlib.sha256()
 with destination.open('rb') as packed_file,source.open('rb') as original:
  for row in sorted({lo,hi-1,*[lo+(hi-lo-1)*i//16 for i in range(17)]}):
   packed_file.seek(4096+(row-lo)*264);actual=packed_file.read(264)
   original.seek(8+length+weight['data_offsets'][0]+row*256);expected_row=original.read(256)
   original.seek(8+length+scale['data_offsets'][0]+row*8);expected_row+=original.read(8)
   assert actual==expected_row,(layer,rank,row)
   samples.update(actual)
 records.append({'layer':layer,'bytes':expected,'reused':False,'sample_rows':17,'sample_sha256':samples.hexdigest()})
 print(json.dumps({'rank':rank,'packed_layer':layer,'bytes':expected}),flush=True)
assert len(records)==2,records
(out/'manifest.json').write_text(json.dumps({'rank':rank,'tp_size':4,'format':'DSV41EN1','records':records},indent=2))
print(json.dumps({'rank':rank,'packed':records}),flush=True)
