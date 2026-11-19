#!/usr/bin/env python3
"""Local anonymous speaker identity; fixed average-link AHC, no API labels used."""
from __future__ import annotations
import contextlib,fcntl,hashlib,json,math,os,sys,tempfile,time,wave
from pathlib import Path
import numpy as np
import sherpa_onnx
import mica_config
MODEL=mica_config.app_support()/'speaker-runtime/wespeaker_en_voxceleb_resnet34_LM.onnx'
NAMES=[f'相手{i}' for i in range(1,5)]
THRESHOLD=.7

def _inside(path,folder):
 path=Path(path).resolve()
 if not path.is_relative_to(folder):raise ValueError('audio outside meeting')
 return path

def _atomic(path,value):
 fd,temp=tempfile.mkstemp(prefix='.speaker-state-',dir=path.parent)
 try:
  with os.fdopen(fd,'w') as f:json.dump(value,f,ensure_ascii=False)
  os.chmod(temp,0o600);os.replace(temp,path)
 finally:
  if os.path.exists(temp):os.unlink(temp)

SEGMENTATION_MODEL=MODEL.with_name('pyannote-segmentation-3.0.onnx')
SEGMENTATION_SHA256='220ad67ca923bef2fa91f2390c786097bf305bceb5e261d4af67b38e938e1079'

def _detector(holder):
 if 'detector' not in holder:
  config=sherpa_onnx.OfflineSpeakerDiarizationConfig(segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(SEGMENTATION_MODEL)),num_threads=1,debug=False,provider='cpu'),embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(MODEL),num_threads=1,debug=False,provider='cpu'),clustering=sherpa_onnx.FastClusteringConfig(num_clusters=-1,threshold=.7),min_duration_on=.1,min_duration_off=.1)
  if not config.validate():raise RuntimeError('overlap detector configuration unavailable')
  holder['detector']=sherpa_onnx.OfflineSpeakerDiarization(config)
 return holder['detector']

def _overlapping(regions):
 points=sorted({region[k] for region in regions for k in ('start','end')})
 overlap=[]
 for a,b in zip(points,points[1:]):
  active={r['speaker'] for r in regions if r['start']<=a and r['end']>=b}
  if len(active)<2:continue
  if overlap and abs(overlap[-1]['end']-a)<1e-6:overlap[-1]['end']=b
  else:overlap.append({'start':a,'end':b})
 return overlap

def _local_regions(detector,samples,rate,offset=0):
 detected=detector.process(np.ascontiguousarray(samples)).sort_by_start_time()
 result=[]
 for segment in detected:
  start=max(0.,float(segment.start))
  end=min(len(samples)/rate,float(segment.end))
  if start<end:result.append({'start':offset+start,'end':offset+end,'speaker':int(segment.speaker)})
 return result

def _overlap_regions(audio,samples,rate,folder,holder):
 if rate!=16000:raise RuntimeError('overlap detector requires 16kHz audio')
 if 'model_sha256' not in holder:
  if not SEGMENTATION_MODEL.is_file():raise RuntimeError('overlap segmentation model unavailable')
  holder['model_sha256']=hashlib.sha256(SEGMENTATION_MODEL.read_bytes()).hexdigest()
  if holder['model_sha256']!=SEGMENTATION_SHA256:raise RuntimeError('overlap segmentation model hash mismatch')
 digest=hashlib.sha256(audio.read_bytes()).hexdigest()
 key=hashlib.sha256((digest+holder['model_sha256']+'sherpa-overlap-context-v2').encode()).hexdigest()
 cache_path=_inside(folder/'.speaker-overlap-cache.json',folder)
 cache=json.loads(cache_path.read_text()) if cache_path.exists() else {'version':1,'entries':[]}
 if not isinstance(cache,dict) or cache.get('version')!=1 or not isinstance(cache.get('entries'),list):raise RuntimeError('invalid overlap cache')
 entries=[]
 for entry in cache['entries']:
  if not isinstance(entry,dict) or not isinstance(entry.get('audio'),str):raise RuntimeError('invalid overlap cache entry')
  if _inside(folder/entry['audio'],folder).is_file():entries.append(entry)
 hit=next((entry for entry in entries if entry.get('key')==key),None)
 if hit is not None:
  regions=hit.get('regions')
  if not isinstance(regions,list) or any(not isinstance(r,dict) or not all(isinstance(r.get(k),(int,float)) and math.isfinite(r[k]) for k in ('start','end')) or not 0<=r['start']<r['end']<=len(samples)/rate+.001 for r in regions):raise RuntimeError('invalid cached overlap regions')
  value={**hit,'audio':str(audio.relative_to(folder))}
 else:
  started=time.perf_counter()
  detector=_detector(holder)
  regions=_overlapping(_local_regions(detector,samples,rate))
  # <=10-second calls use pyannote's local speaker slots, before identity clustering.
  # Their acoustic context is independent of ASR sentence boundaries.
  context_windows=[]
  last=max(0,len(samples)-rate*10)
  starts=list(range(0,last+1,rate*5))
  if starts[-1]!=last:starts.append(last)
  for begin in starts:
   finish=min(len(samples),begin+rate*10)
   local=_local_regions(detector,samples[begin:finish],rate,begin/rate)
   context_windows.append({'start':begin/rate,'end':finish/rate,'regions':local})
  elapsed=time.perf_counter()-started
  value={'key':key,'audio':str(audio.relative_to(folder)),'audio_sha256':digest,'model_sha256':holder['model_sha256'],'detector':'sherpa-pyannote-segmentation-3.0-overlap-v1','regions':regions,'context_windows':context_windows,'duration_seconds':len(samples)/rate,'inference_seconds':elapsed}
 # Keep one bounded file and no inference cache for deleted audio files.
 entries=[entry for entry in entries if entry.get('key')!=key]
 _atomic(cache_path,{'version':1,'entries':(entries+[value])[-3:]})
 return value

def _extractor():
 config=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(MODEL),num_threads=1,debug=False,provider='cpu')
 if not config.validate():raise RuntimeError('speaker model unavailable')
 return sherpa_onnx.SpeakerEmbeddingExtractor(config)


def _voice_pcm(samples,rate,regions,start,end):
 pieces=[samples[round(max(start,r['start'])*rate):round(min(end,r['end'])*rate)]
         for r in regions if max(start,r['start'])<min(end,r['end'])]
 return np.concatenate(pieces) if pieces else samples[:0]

def _context_embedding(extractor,samples,rate,start,end,overlap,seam=None):
 windows=overlap.get('context_windows',[])
 if not windows:return None
 window=min(windows,key=lambda value:abs((value['start']+value['end'])/2-(start+end)/2))
 votes={}
 for region in window['regions']:
  duration=max(0,min(end,region['end'])-max(start,region['start']))
  votes[region['speaker']]=votes.get(region['speaker'],0.)+duration
 total=sum(votes.values())
 if total<=0 or max(votes.values())<.6*total:return None
 speaker=max(votes,key=votes.get)
 regions=[r for r in window['regions'] if r['speaker']==speaker]
 # Never use a detected simultaneous-voice region as a representative context.
 if any(min(r['end'],o['end'])-max(r['start'],o['start'])>.1 for r in regions for o in overlap['regions']):return None
 if seam is None:
  context=_voice_pcm(samples,rate,regions,0,len(samples)/rate)
 else:
  # One local speaker slot can incorrectly join a change of voice at the cut.
  # Require independent embeddings from real PCM on both sides of the cut.
  if votes[speaker]<.6*(end-start) or total-votes[speaker]>.1:return None
  left=_voice_pcm(samples,rate,regions,seam-3,seam)
  right=_voice_pcm(samples,rate,regions,seam,seam+3)
  if min(len(left),len(right))<rate:return None
  a,b=_embedding(extractor,left,rate),_embedding(extractor,right,rate)
  if a is None or b is None or float(a@b)<THRESHOLD:return None
  context=np.concatenate([_voice_pcm(samples,rate,regions,0,start),
                          _voice_pcm(samples,rate,regions,end,len(samples)/rate)])
 if len(context)<rate:return None
 vector=_embedding(extractor,context,rate)
 utterance=_embedding(extractor,samples[round(start*rate):round(end*rate)],rate)
 # A local segmentation error cannot lend another voice to this sentence.
 if vector is None or utterance is None or float(vector@utterance)<THRESHOLD:return None
 return (vector,utterance) if seam is not None else vector

def _seam_context(samples,rate,offset,next_samples,next_rate,next_offset,holder):
 if next_rate!=rate or round((next_offset-offset)*rate)!=len(samples):return None
 tail=samples[-5*rate:]
 joined=np.concatenate([tail,next_samples[:5*rate]])
 origin=offset+(len(samples)-len(tail))/rate
 regions=_local_regions(_detector(holder),joined,rate)
 overlap={'regions':_overlapping(regions),'context_windows':[{'start':0.,'end':len(joined)/rate,'regions':regions}],
          'detector':'sherpa-pyannote-segmentation-3.0-overlap-v1','model_sha256':holder['model_sha256']}
 return joined,origin,len(tail)/rate,overlap

def _mark_overlap(item,overlap,offset,start,end):
 intersections=[{'start':max(start,offset+r['start']),'end':min(end,offset+r['end'])}
                for r in overlap['regions'] if min(end,offset+r['end'])>max(start,offset+r['start'])]
 overlap_seconds=sum(r['end']-r['start'] for r in intersections)
 if overlap_seconds>.1:
  item['overlap_review']={'status':'unresolved','reason':'overlapping_voices','detector':overlap['detector'],'model_sha256':overlap['model_sha256'],'overlap_seconds':overlap_seconds,'regions':intersections}

def _embedding(extractor,samples,rate):
 stream=extractor.create_stream();stream.accept_waveform(sample_rate=rate,waveform=np.ascontiguousarray(samples));stream.input_finished()
 if not extractor.is_ready(stream):return None
 vector=np.asarray(extractor.compute(stream),dtype=np.float32);norm=float(np.linalg.norm(vector))
 if not np.isfinite(vector).all() or norm<=0:raise ValueError('invalid embedding')
 return vector/norm

def _clusters(vectors):
 if not vectors:return []
 matrix=np.stack(vectors);similarities=(matrix@matrix.T).astype(np.float64)
 count=len(vectors);sizes=np.ones(count,dtype=np.int64);members={i:[i] for i in range(count)}
 np.fill_diagonal(similarities,-np.inf)
 while len(members)>1:
  # np.argmax does the dense search in C. Original indices preserve deterministic ties.
  flat=int(np.argmax(similarities));i,j=np.unravel_index(flat,similarities.shape)
  if similarities[i,j]<THRESHOLD:break
  if i>j:i,j=j,i
  total=sizes[i]+sizes[j]
  row=(similarities[i]*sizes[i]+similarities[j]*sizes[j])/total
  similarities[i]=row;similarities[:,i]=row
  members[i]+=members.pop(j);sizes[i]=total
  similarities[j,:]=-np.inf;similarities[:,j]=-np.inf;similarities[i,i]=-np.inf
 return [members[i] for i in sorted(members)]

def _matching(clusters,names,votes):
 states={0:(0.,{})}
 for cluster in clusters:
  updated=dict(states)
  for mask,(weight,mapping) in states.items():
   for i,name in enumerate(names):
    vote=votes.get((cluster,name),0.)
    if mask&(1<<i) or vote<=0:continue
    new=mask|(1<<i);value=weight+vote
    if new not in updated or value>updated[new][0]:updated[new]=(value,{**mapping,cluster:name})
  states=updated
 return max(states.values(),key=lambda v:v[0])[1]

def _validate_state(state):
 if not isinstance(state,dict) or state.get('version')!=2:raise ValueError('invalid speaker state version')
 speakers=state.get('speakers');seen=state.get('seen_samples');segments=state.get('segments')
 if not isinstance(speakers,dict) or any(name not in NAMES for name in speakers) or not isinstance(seen,dict) or not isinstance(segments,list):raise ValueError('invalid speaker state')
 sums={};counts={name:0 for name in speakers};dimension=None
 for key,sample in seen.items():
  if not isinstance(key,str) or not isinstance(sample,dict) or sample.get('name') not in speakers:raise ValueError('invalid speaker sample')
  vector=np.asarray(sample.get('vector'),dtype=np.float64)
  if vector.ndim!=1 or not vector.size or not np.isfinite(vector).all() or not np.isclose(np.linalg.norm(vector),1,atol=1e-4):raise ValueError('invalid saved vector')
  if dimension is not None and vector.size!=dimension:raise ValueError('inconsistent vector dimensions')
  dimension=vector.size;name=sample['name'];counts[name]+=1;sums[name]=sums.get(name,np.zeros_like(vector))+vector
 for name,value in speakers.items():
  if not isinstance(value,dict) or type(value.get('sample_count')) is not int or value['sample_count']!=counts[name]:raise ValueError('invalid speaker count')
  if not counts[name]:
   if value.get('centroid') is not None or value.get('embedding_sum') is not None:raise ValueError('nonempty zero-count speaker')
   continue
  total=np.asarray(value.get('embedding_sum'),dtype=np.float64);centroid=np.asarray(value.get('centroid'),dtype=np.float64)
  if total.shape!=sums[name].shape or centroid.shape!=total.shape or not np.isfinite(total).all() or not np.isfinite(centroid).all() or not np.allclose(total,sums[name],atol=1e-5) or np.linalg.norm(total)<=0 or not np.allclose(centroid,total/np.linalg.norm(total),atol=1e-5):raise ValueError('invalid speaker aggregate')
 for segment in segments:
  if not isinstance(segment,dict) or segment.get('who') not in [*speakers,'話者未確定'] or not isinstance(segment.get('key'),str) or not isinstance(segment.get('audio'),str) or not all(isinstance(segment.get(k),(int,float)) and math.isfinite(segment[k]) for k in ('start','end','offset')) or segment['start']>segment['end']:raise ValueError('invalid saved segment')

def _update_sample(state,key,desired):
 seen=state['seen_samples'];speakers=state['speakers'];old=seen.get(key)
 if old is not None and desired is not None and old['name']==desired['name'] and old['vector']==desired['vector']:return
 if old is not None:
  name=old['name'];value=speakers[name];count=value['sample_count']-1
  if count==0:value.update(sample_count=0,embedding_sum=None,centroid=None)
  else:
   total=np.asarray(value['embedding_sum'],dtype=np.float64)-np.asarray(old['vector'],dtype=np.float64)
   value.update(sample_count=count,embedding_sum=total.tolist(),centroid=(total/np.linalg.norm(total)).tolist())
  del seen[key]
 if desired is not None:
  name=desired['name'];value=speakers[name];vector=np.asarray(desired['vector'],dtype=np.float64)
  if value['embedding_sum'] is not None:vector=vector+np.asarray(value['embedding_sum'],dtype=np.float64)
  value.update(sample_count=value['sample_count']+1,embedding_sum=vector.tolist(),centroid=(vector/np.linalg.norm(vector)).tolist());seen[key]=desired

def relabel(request):
 original_folder=Path(request['folder'])
 if not original_folder.is_absolute():raise ValueError('meeting folder must be absolute')
 folder=original_folder.resolve()
 if not folder.is_dir():raise ValueError('meeting folder missing')
 batches=request['batches']
 if not isinstance(batches,list) or not 1<=len(batches)<=3:raise ValueError('expected up to three batches')
 lock_path=_inside(folder/'.speaker-identity.lock',folder)
 with lock_path.open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX)
  final=request.get('final',False)
  if not isinstance(final,bool):raise ValueError('invalid final flag')
  return _relabel_locked(folder,batches,final)

def _relabel_locked(folder,batches,final=False):
 state_path=_inside(folder/('.speaker-final-identity.json' if final else '.speaker-identity.json'),folder)
 state=json.loads(state_path.read_text()) if state_path.exists() and not final else {'version':2,'speakers':{},'segments':[],'seen_samples':{}}
 _validate_state(state)
 speakers=state['speakers']
 output=[];rows=[];vectors=[];extractor=None;ids=set();overlap_holder={}
 inputs=[];seams={};seam_vectors=[]
 for batch in sorted(batches,key=lambda value:float(value['offset'])):
  relative=Path(batch['audio'])
  if relative.is_absolute():raise ValueError('absolute audio path')
  audio=_inside(folder/relative,folder);offset=float(batch['offset'])
  if not math.isfinite(offset) or offset<0:raise ValueError('invalid offset')
  with wave.open(str(audio),'rb') as f:
   if f.getnchannels()!=1 or f.getsampwidth()!=2:raise ValueError('expected mono PCM16 WAV')
   rate=f.getframerate();samples=np.frombuffer(f.readframes(f.getnframes()),dtype='<i2').astype(np.float32)/32768
  inputs.append((batch,relative,audio,offset,samples,rate))
 for index,(batch,relative,audio,offset,samples,rate) in enumerate(inputs):
  overlap=_overlap_regions(audio,samples,rate,folder,overlap_holder)
  current={**batch,'segments':[]};output.append(current)
  for source in sorted(batch['segments'],key=lambda value:(float(value['start']),value['id'])):
   item=dict(source);start,end=float(item['start']),float(item['end'])
   if not isinstance(item['id'],str) or item['id'] in ids or not(math.isfinite(start) and math.isfinite(end)) or start<0 or end<start:raise ValueError('invalid segment')
   ids.add(item['id']);item.update(who='話者未確定',speaker_status='tentative');current['segments'].append(item)
   a=max(0,start-offset);b=min(len(samples)/rate,end-offset)
   row={'item':item,'audio':str(relative),'offset':offset,'start':start,'end':end,'duration':max(0,b-a),'key':hashlib.sha256(json.dumps([str(relative),item['id'],start,end],ensure_ascii=False).encode()).hexdigest()}
   rows.append(row)
   _mark_overlap(item,overlap,offset,start,end)
   if any(item.get(key,{}).get('status')=='unresolved' for key in ('overlap_review','alignment_review')):continue
   if round((end-offset)*rate)>len(samples):
    if index+1==len(inputs) or end-start<1 or item.get('capture_review',{}).get('status')=='unresolved':continue
    if index not in seams:
     _,_,_,next_offset,next_samples,next_rate=inputs[index+1]
     seams[index]=_seam_context(samples,rate,offset,next_samples,next_rate,next_offset,overlap_holder)
    if seams[index] is None:continue
    joined,origin,seam,seam_overlap=seams[index]
    left,right=start-origin,end-origin
    if left<0 or round(right*rate)>len(joined):continue
    _mark_overlap(item,seam_overlap,origin,start,end)
    if item.get('overlap_review',{}).get('status')=='unresolved':continue
    if extractor is None:extractor=_extractor()
    vector=_context_embedding(extractor,joined,rate,left,right,seam_overlap,seam=seam)
    if vector is not None:seam_vectors.append((item,vector))
    continue
   if b-a<1:continue
   if extractor is None:extractor=_extractor()
   vector=_context_embedding(extractor,samples,rate,a,b,overlap)
   if vector is not None:row['vector_index']=len(vectors);vectors.append(vector)
 groups=_clusters(vectors);retained={};centroids={};row_by_vector={r['vector_index']:r for r in rows if 'vector_index' in r}
 for i,members in enumerate(groups):
  if sum(row_by_vector[j]['duration'] for j in members)<(4 if final else 2):continue
  retained[i]=members;centroid=np.mean([vectors[j] for j in members],axis=0);centroids[i]=centroid/np.linalg.norm(centroid)
 votes={}
 for cluster,members in retained.items():
  for j in members:
   row=row_by_vector[j]
   for previous in state.get('segments',[]):
    name=previous['who']
    if name not in speakers:continue
    duration=max(0,min(row['end'],previous['end'])-max(row['start'],previous['start']))
    if duration:votes[(cluster,name)]=votes.get((cluster,name),0.)+duration
 # Conflicting old identities within one cluster are uncertain, not evidence to merge people.
 eligible=[i for i in retained if sum(votes.get((i,name),0)>0 for name in speakers)<=1]
 labels=_matching(eligible,sorted(speakers),votes);voted_names=set(labels.values());used=set(labels.values());blocked=set(retained)-set(eligible)
 for cluster in retained:
  if cluster in labels or cluster in blocked:continue
  if any(votes.get((cluster,name),0)>0 for name in speakers):blocked.add(cluster);continue
  similarities=[]
  for name,value in speakers.items():
   if value.get('centroid') is not None:similarities.append((float(centroids[cluster]@np.asarray(value['centroid'],dtype=np.float32)),name))
  similarities.sort(reverse=True)
  if similarities and similarities[0][0]>=THRESHOLD:
   best,name=similarities[0];margin=best-(similarities[1][0] if len(similarities)>1 else -1.)
   strong=sum(row_by_vector[j]['duration']>=2 for j in retained[cluster])>=2
   separated_from_voted=name in voted_names and strong and not any(score>=THRESHOLD and other not in used for score,other in similarities)
   if name not in used and margin>=.05:labels[cluster]=name;used.add(name)
   elif not separated_from_voted:blocked.add(cluster)
   # AHC separated two supported co-occurring voices; an already-voted name cannot erase the new one.
  elif any(value.get('centroid') is None for value in speakers.values()):blocked.add(cluster)
 # First observations receive a new anonymous ID, in chronological order.
 for cluster in sorted(retained,key=lambda i:min(row_by_vector[j]['start'] for j in retained[i])):
  if cluster in labels or cluster in blocked:continue
  available=next((name for name in NAMES if name not in speakers and name not in used),None)
  if available:
   labels[cluster]=available;used.add(available);speakers[available]={'centroid':None,'sample_count':0,'embedding_sum':None}
 for cluster,name in labels.items():
  for j in retained[cluster]:row_by_vector[j]['item'].update(who=name,speaker_status='confirmed')
 # Boundary recovery can confirm an existing voice but must not train it or
 # change the clusters that established its identity.
 for item,(context,utterance) in seam_vectors:
  matches=sorted(((float(context@center),cluster) for cluster,center in centroids.items()),reverse=True)
  if not matches:continue
  score,cluster=matches[0]
  if score<THRESHOLD or cluster not in labels or (len(matches)>1 and score-matches[1][0]<.05):continue
  # The centroid can hide inconsistent members in an average-link cluster.
  if any(min(float(context@vectors[j]),float(utterance@vectors[j]))<THRESHOLD for j in retained[cluster]):continue
  item.update(who=labels[cluster],speaker_status='confirmed')
 # A revised input may retract a prior assignment. Remove its exact old contribution first.
 current_keys={row['key'] for row in rows};current_audio={(str(Path(batch['audio'])),float(batch['offset'])) for batch in batches}
 for previous in state['segments']:
  if (previous['audio'],previous['offset']) in current_audio and previous['key'] not in current_keys:_update_sample(state,previous['key'],None)
 for row in rows:
  name=row['item']['who'];desired=None
  if name in speakers and row['duration']>=2 and 'vector_index' in row:
   desired={'name':name,'vector':vectors[row['vector_index']].tolist()}
  _update_sample(state,row['key'],desired)
 state['segments']=[{'id':r['item']['id'],'start':r['start'],'end':r['end'],'who':r['item']['who'] if 'vector_index' in r else '話者未確定','key':r['key'],'audio':r['audio'],'offset':r['offset']} for r in rows]
 _validate_state(state)
 _atomic(state_path,state)
 return {'batches':output}

def main():
 try:
  request=json.load(sys.stdin)
  with contextlib.redirect_stdout(sys.stderr):result=relabel(request)
  json.dump(result,sys.stdout,ensure_ascii=False);sys.stdout.write('\n')
 except Exception:
  print('ローカル話者判定に失敗しました。音声と設定を確認してください。',file=sys.stderr);return 1
 return 0
if __name__=='__main__':raise SystemExit(main())
