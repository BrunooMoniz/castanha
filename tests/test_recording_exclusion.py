import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from castanha.annotations import save_annotations
from castanha.bronze_ingest import (build_transcript_request, build_synthesis_request, revision_fingerprint,
    frozen_destination, synthesis_origin)
from castanha.durability import write_json, file_sha256
from castanha.engine import CastanhaEngine
from castanha.recording_exclusion import (ARCHIVE, POINTER, ExclusionError, ExclusionConflict,
    exclude_recording, restore_recording, resume_exclusion)
from castanha.storage import MeetingStorage
from castanha.sync import pending_candidates, sync_meeting
from castanha.zinom_adapter import ZinomAdapter

GOLD = {'facts': [], 'decisions': [], 'action_items': [], 'people_notes': []}
ACK = {'ok': True, 'tombstoned': True, 'revisions': 1, 'contentHashes': 0,
       'chunks': 2, 'facts': 0, 'profileFacts': 0, 'rechecks': 0}


class TestRecordingExclusion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.base = self.root / 'meetings'
        config = self.root / 'config/castanha'; config.mkdir(parents=True)
        self.zcfg = {'enabled': True, 'token': 'fixture-token', 'bronze_ingest_enabled': True,
                     'endpoint': 'https://fixture.invalid/mcp', 'workspace': 'personal', 'account_id': None}
        (config / 'config.json').write_text(json.dumps({'storage': {'base_dir': str(self.base),
            'bronze_dir': str(self.base/'bronze'), 'silver_dir': str(self.base/'silver'), 'gold_dir': str(self.base/'gold')},
            'llm': {'provider': 'groq', 'api_key': ''}, 'zinom': self.zcfg}))
        for guard in (patch.dict(os.environ, {'XDG_CONFIG_HOME':str(self.root/'config'), 'XDG_STATE_HOME':str(self.root/'state')}),
                      patch('urllib.request.urlopen', side_effect=AssertionError('No live network')),
                      patch('castanha.engine.get_transcriber', side_effect=AssertionError('No ASR for completed survivors'))):
            guard.start(); self.addCleanup(guard.stop)
        self.storage = MeetingStorage(self.base)
        self.slug = 'fixture-meeting'; self.bronze = self.storage.bronze_dir/self.slug
        self.bronze.mkdir(); (self.bronze/'.jobs').mkdir()
        records = []
        for n in (1,2):
            audio = self.bronze/f'capture_job{n}.ogg'; audio.write_bytes(f'original audio {n}'.encode())
            rec = {'id':audio.name, 'filename':audio.name, 'job_id':f'job{n}', 'sha256':file_sha256(audio),
                'transcribed':True, 'transcription_provider':'fixture', 'audio_status':'ok', 'duration_seconds':10,
                'recorded_at':f'2026-09-11T10:0{n}:00+00:00'}
            records.append(rec)
            write_json(self.bronze/'.jobs'/f'job{n}.json', {'id':f'job{n}', 'audio_path':str(audio),
                'sha256':rec['sha256'], 'stage':'done', 'recorded_at':rec['recorded_at'], 'provider':'fixture',
                'transcript':f'CONTEUDO {n}', 'state':{'mode':'dual', 'current_meeting':{'title':'Fixture'}},
                'duration_seconds':10,'audio_status':'ok','utterances':[]})
        self.metadata = {'slug':self.slug, 'title':'Fixture', 'recorded_at':'2026-09-11T10:00:00+00:00',
            'recordings':records, 'recording_revision':0, 'mode':'dual', 'audio_status':'ok',
            'processing_status':'complete', 'transcription_provider':'fixture', 'memory_recording_ids':[r['id'] for r in records],
            'zinom':{'status':'error'}}
        write_json(self.bronze/'metadata.json', self.metadata)
        (self.bronze/'transcript_raw.txt').write_text('CONTEUDO 1\nCONTEUDO 2')
        (self.bronze/'transcript_segments.json').write_text('{"old":"both"}')
        self.storage.save_silver(self.slug,'RESUMO ANTIGO DE AMBOS')
        self.storage.save_gold(self.slug,{'old':True})
        save_annotations(self.slug,'Minhas notas autorais',self.storage)
        self.engine = CastanhaEngine(); self.engine.storage = self.storage
        self.engine.summarizer.generate_silver = Mock(side_effect=lambda meta,text: 'RESUMO NOVO '+text)
        self.engine.summarizer.generate_gold = Mock(return_value=GOLD)
        self.engine.zinom.ingest_meeting = Mock(return_value={'status':'ok'})

    def exclude(self, **kwargs):
        return exclude_recording(self.slug,'capture_job1.ogg',self.storage,**kwargs)

    def one_audio(self):
        (self.bronze/'capture_job2.ogg').unlink(); (self.bronze/'.jobs/job2.json').unlink()
        self.metadata['recordings']=self.metadata['recordings'][:1]
        self.metadata['memory_recording_ids']=self.metadata['memory_recording_ids'][:1]
        write_json(self.bronze/'metadata.json',self.metadata)

    def receipts(self):
        directory=self.bronze/'.brain-ingest';directory.mkdir()
        write_json(directory/'destination.json',frozen_destination(endpoint=self.zcfg['endpoint'],token=self.zcfg['token'],workspace='personal'))
        self.sources=[]
        for rec in self.metadata['recordings']:
            request=build_transcript_request(self.slug,{**self.metadata,'recordings':[rec]},'CONTEUDO '+rec['job_id'][-1],
                captured_at='2026-09-11T12:00:00+00:00',workspace='personal',recording_id=rec['job_id'])
            self.sources.append(request['envelope']['source_id']); self.receipt(directory,request)
        synth=build_synthesis_request(self.slug,self.metadata,'RESUMO ANTIGO DE AMBOS',GOLD,
            captured_at='2026-09-11T12:00:00+00:00',workspace='personal',origin_ids=[r['job_id'] for r in self.metadata['recordings']])
        self.sources.append(synth['envelope']['source_id']);self.receipt(directory,synth)
        self.metadata['zinom']={'status':'ok'};write_json(self.bronze/'metadata.json',self.metadata)

    @staticmethod
    def receipt(directory, request):
        write_json(directory/(revision_fingerprint(request)+'.json'), {'request':request,'status':'ok','attempted':True,
            'remote_identity':{'job_id':10,'revision_id':11},'result':{'status':'ok'}})

    def client(self, reply=ACK):
        client=Mock();client.call_tool.return_value={'content':[{'type':'text','text':json.dumps(reply)}]}
        return client


    def test_summary_quota_keeps_rebuild_pending_without_republishing(self):
        from castanha.summarizer import LlmUnavailable
        self.exclude()
        self.engine.summarizer.generate_silver.side_effect=LlmUnavailable('fixture quota')
        result=self.engine.reprocess_meeting(self.slug)
        self.assertEqual(result['status'],'partial')
        self.assertEqual(self.storage.get_meeting(self.slug)['content_status'],'rebuilding')
        self.assertFalse((self.storage.silver_dir/(self.slug+'.md')).exists())
        self.engine.zinom.ingest_meeting.assert_not_called()
        self.assertIn(('',self.slug),pending_candidates(self.storage))

    def test_delivery_pending_reports_local_complete_without_false_remote_success(self):
        self.exclude()
        self.engine.zinom.ingest_meeting.return_value={'status':'pending','reason':'fixture queue'}
        result=self.engine.reprocess_meeting(self.slug)
        self.assertEqual(result['status'],'partial')
        self.assertIn('entrega ao Zinom pendente',result['message'])
        self.assertEqual(self.storage.get_meeting(self.slug)['content_status'],'current')
        self.assertEqual(self.storage.get_meeting(self.slug)['zinom']['status'],'pending')

    def test_corrupt_control_does_not_abort_inventory(self):
        from castanha.sync import sync_pending
        (self.bronze/POINTER).write_bytes(b'invalid-json')
        other=self.storage.bronze_dir/'second';other.mkdir()
        write_json(other/'metadata.json',{'slug':'second','zinom':{'status':'error'}})
        inventory=pending_candidates(self.storage)
        self.assertIn(('',self.slug),inventory)
        self.assertIn(('', 'second'),inventory)
        result=sync_pending(storage=self.storage)
        self.assertEqual(next(r for r in result if r['slug']==self.slug)['status'],'error')

    def test_changed_credentials_before_attempt_still_allows_local_restore(self):
        self.one_audio();self.receipts();excluded=self.exclude()
        adapter=ZinomAdapter();adapter.token='different-fixture'
        with patch('castanha.zinom_adapter.ZinomMcpClient') as factory:
            pending=resume_exclusion(self.slug,self.storage,adapter=adapter)
            factory.assert_not_called()
        self.assertTrue(pending['can_restore'])
        self.assertEqual(restore_recording(self.slug,excluded['exclusion_id'],self.storage)['status'],'ok')

    def test_cleanup_error_category_is_safe_and_durable(self):
        self.one_audio();self.receipts();excluded=self.exclude()
        client=self.client({'ok':False,'error':'workspace_forbidden','message':'fixture SECRET'})
        with patch('castanha.zinom_adapter.ZinomMcpClient',return_value=client):
            pending=resume_exclusion(self.slug,self.storage)
        self.assertIn('workspace_forbidden',pending['cleanup_reason'])
        self.assertNotIn('SECRET',json.dumps(pending))
        op=json.loads((self.bronze/ARCHIVE/excluded['exclusion_id']/'operation.json').read_text())
        self.assertEqual(op['last_error_code'],'workspace_forbidden')

    def test_old_annotation_regeneration_checkpoint_is_archived(self):
        checkpoint=self.bronze/'.annotations-regeneration.json'
        checkpoint.write_text('{"stage":"old","silver":"excluded content"}')
        result=self.exclude()
        self.assertFalse(checkpoint.exists())
        self.engine.reprocess_meeting(self.slug)
        self.assertNotIn('excluded content',(self.storage.silver_dir/(self.slug+'.md')).read_text())
        op=json.loads((self.bronze/ARCHIVE/result['exclusion_id']/'operation.json').read_text())
        self.assertTrue(any(item['parts']==['.annotations-regeneration.json'] and item['sha256'] for item in op['files']))

    def test_new_capture_after_empty_meeting_uses_new_revision_and_not_old_operation(self):
        from castanha.recording_exclusion import applicable
        self.one_audio();self.exclude()
        from castanha.audio import ChannelLevels
        from castanha.transcription import TranscriptionResult
        audio=self.root/'new-capture.ogg';audio.write_bytes(b'new capture')
        self.engine.state_mgr.write({'status':'recording','pid':None,'audio_path':str(audio),
            'target_meeting_slug':self.slug,'capture_slug':None,'capture_job_id':'newjob',
            'current_meeting':{'title':'Fixture'},'mode':'mic-only'})
        with patch('castanha.engine.get_transcriber') as provider, \
             patch('castanha.engine.measure_channel_levels',return_value=[ChannelLevels(0,'mic',-30,-10,False)]), \
             patch('castanha.engine.probe_duration_seconds',return_value=10),patch('castanha.engine.notify'):
            provider.return_value.transcribe.return_value=TranscriptionResult('NEW RECORDING',[],'fixture',{})
            result=self.engine.stop_recording()
            provider.return_value.transcribe.assert_called_once()
        self.assertFalse(applicable(self.slug,self.storage))
        self.assertEqual(result['status'],'success')
        self.assertEqual(self.storage.read_transcript(self.slug),'NEW RECORDING')
        self.assertEqual(self.storage.get_meeting(self.slug)['content_status'],'current')
        self.assertEqual(self.storage.get_meeting(self.slug)['recording_revision'],2)
        self.assertEqual(self.storage.get_meeting(self.slug)['remaining_count'],1)
        self.assertFalse(self.storage.get_meeting(self.slug)['can_restore'])
        self.assertEqual([r['job_id'] for r in self.storage._read_bronze_metadata(self.slug)['recordings']],['newjob'])

    def test_capture_refused_during_interrupted_local_exclusion_before_recorder(self):
        from castanha import recording_exclusion as module
        original=os.unlink
        def interrupted(name,*args,**kwargs):
            if name=='transcript_raw.txt':raise OSError('fixture crash')
            return original(name,*args,**kwargs)
        with patch.object(module.os,'unlink',side_effect=interrupted):
            with self.assertRaises(OSError):self.exclude()
        with patch.object(self.engine.recorder,'start') as recorder:
            result=self.engine.start_recording(meeting_slug=self.slug)
            self.assertEqual(result['status'],'error')
            recorder.assert_not_called()

    def test_exclude_preserves_quarantine_notes_and_hides_all_old_derivatives(self):
        result=self.exclude(expected_revision=0)
        self.assertEqual(result['content_status'],'invalidated');self.assertEqual(result['remaining_count'],1)
        self.assertTrue(result['can_restore'])
        self.assertEqual(result['recording_revision'],1)
        for path in (self.bronze/'capture_job1.ogg',self.bronze/'.jobs/job1.json',self.bronze/'transcript_raw.txt',
                     self.storage.silver_dir/(self.slug+'.md'),self.storage.gold_dir/(self.slug+'.json')):
            self.assertFalse(path.exists(),str(path))
        self.assertEqual((self.bronze/ARCHIVE/result['exclusion_id']/'audio').read_bytes(),b'original audio 1')
        self.assertEqual((self.bronze/'annotations.md').read_text(),'Minhas notas autorais')
        self.assertEqual(self.storage.get_meeting(self.slug)['content_status'],'invalidated')
        self.assertEqual(pending_candidates(self.storage),[])

    def test_rebuild_reuses_only_surviving_job_and_never_removed_transcript(self):
        self.exclude()
        result=self.engine.reprocess_meeting(self.slug)
        self.assertEqual(result['status'],'success')
        self.assertEqual(self.storage.read_transcript(self.slug),'CONTEUDO 2')
        self.assertNotIn('CONTEUDO 1',(self.storage.silver_dir/(self.slug+'.md')).read_text())
        metadata=self.storage._read_bronze_metadata(self.slug)
        self.assertEqual([r['job_id'] for r in metadata['recordings']],['job2'])
        self.assertEqual(metadata['content_status'],'current')
        self.assertEqual(self.engine.summarizer.generate_silver.call_args.args[0]['manual_annotations'],'Minhas notas autorais')

    def test_zero_audio_keeps_meeting_empty_without_asr_llm_or_zinom(self):
        self.one_audio();result=self.exclude()
        self.assertEqual(result['content_status'],'empty');self.assertFalse(result['can_reprocess'])
        result=self.engine.reprocess_meeting(self.slug)
        self.assertEqual(result['status'],'empty')
        self.engine.summarizer.generate_silver.assert_not_called();self.engine.zinom.ingest_meeting.assert_not_called()
        self.assertTrue(self.bronze.exists());self.assertEqual(pending_candidates(self.storage),[])

    def test_restore_keeps_new_author_notes_and_restores_pipeline_bytes(self):
        before={p: p.read_bytes() for p in (self.bronze/'transcript_raw.txt',self.bronze/'transcript_segments.json',
                self.storage.silver_dir/(self.slug+'.md'),self.storage.gold_dir/(self.slug+'.json'),self.bronze/'.jobs/job1.json')}
        result=self.exclude();save_annotations(self.slug,'Notas novas durante exclusão',self.storage)
        restored=restore_recording(self.slug,result['exclusion_id'],self.storage)
        self.assertEqual(restored['status'],'ok');self.assertEqual(restored['recording_revision'],2)
        self.assertEqual({p:p.read_bytes() for p in before},before)
        self.assertEqual((self.bronze/'annotations.md').read_text(),'Notas novas durante exclusão')
        self.assertTrue((self.bronze/'capture_job1.ogg').exists())

    def test_optimistic_and_path_guards_make_no_changes(self):
        before=(self.bronze/'metadata.json').read_bytes()
        with self.assertRaises(ExclusionConflict):self.exclude(expected_revision=1)
        for slug,name in (('../escape','capture_job1.ogg'),(self.slug,'../capture_job1.ogg')):
            with self.assertRaises((ExclusionError,ValueError,OSError)):exclude_recording(slug,name,self.storage)
        (self.bronze/'capture_job1.ogg').unlink();(self.bronze/'capture_job1.ogg').symlink_to(self.bronze/'capture_job2.ogg')
        with self.assertRaises(ExclusionError):self.exclude()
        self.assertEqual((self.bronze/'metadata.json').read_bytes(),before)

    def test_restore_rejects_changed_metadata_or_new_derivative(self):
        result=self.exclude();self.storage.save_silver(self.slug,'new unrelated result')
        with self.assertRaises(ExclusionConflict):restore_recording(self.slug,result['exclusion_id'],self.storage)
        self.assertFalse((self.bronze/'capture_job1.ogg').exists())

    def test_cleanup_exact_audio_and_synthesis_ack_before_remaining_pipeline(self):
        self.receipts();result=self.exclude()
        self.assertEqual(result['cleanup_status'],'pending')
        client=self.client()
        with patch('castanha.zinom_adapter.ZinomMcpClient',return_value=client):
            result=resume_exclusion(self.slug,self.storage,engine=self.engine,reprocess=True)
        targets=[call.args[1] for call in client.call_tool.call_args_list]
        self.assertEqual({t['source_id'] for t in targets},{self.sources[0],self.sources[-1]})
        self.assertTrue(all(t['workspace']=='personal' and t['confirm'] is True and 'account_id' not in t for t in targets))
        self.assertEqual(result['cleanup_status'],'complete');self.assertEqual(result['content_status'],'current')
        self.assertFalse(result['can_restore'])
        with self.assertRaises(ExclusionError):restore_recording(self.slug,result['exclusion_id'],self.storage)
        self.assertEqual(self.storage.read_transcript(self.slug),'CONTEUDO 2')

    def test_lost_forget_reply_retries_same_scope_and_blocks_false_success(self):
        self.one_audio();self.receipts();removed=self.exclude()
        client=self.client();client.call_tool.side_effect=OSError('lost response')
        with patch('castanha.zinom_adapter.ZinomMcpClient',return_value=client):
            pending=sync_meeting(self.slug,self.storage)
        first=client.call_tool.call_args.args
        self.assertEqual(pending['status'],'pending');self.assertFalse(pending['can_restore'])
        self.assertEqual(self.storage._read_bronze_metadata(self.slug)['zinom']['status'],'pending_cleanup')
        client=self.client({**ACK,'chunks':4})
        with patch('castanha.zinom_adapter.ZinomMcpClient',return_value=client):
            result=sync_meeting(self.slug,self.storage)
        self.assertEqual(client.call_tool.call_args_list[0].args,first)
        self.assertEqual(result['cleanup_status'],'complete');self.assertEqual(result['content_status'],'empty')
        self.assertEqual(pending_candidates(self.storage),[])

    def test_bad_ack_or_changed_credentials_remain_pending(self):
        self.one_audio();self.receipts();self.exclude()
        client=self.client({'ok':True})
        with patch('castanha.zinom_adapter.ZinomMcpClient',return_value=client):
            self.assertEqual(sync_meeting(self.slug,self.storage)['status'],'pending')
        adapter=ZinomAdapter();adapter.token='another-credential'
        with patch('castanha.zinom_adapter.ZinomMcpClient') as factory:
            self.assertEqual(resume_exclusion(self.slug,self.storage,adapter=adapter)['status'],'pending')
            factory.assert_not_called()

    def test_interrupted_exclusion_can_resume_without_reviving_old_content(self):
        from castanha import recording_exclusion as module
        real_unlink=os.unlink
        def interrupted(name,*args,**kwargs):
            if name=='transcript_raw.txt': raise OSError('fixture interruption')
            return real_unlink(name,*args,**kwargs)
        with patch.object(module.os,'unlink',side_effect=interrupted):
            with self.assertRaises(OSError):self.exclude()
        self.assertTrue((self.bronze/'transcript_raw.txt').exists())
        self.assertEqual(self.storage.get_meeting(self.slug)['content_status'],'invalidated')
        from castanha.annotations import regenerate_annotations, AnnotationError
        with self.assertRaises(AnnotationError):
            regenerate_annotations(self.slug,self.storage,summarizer=self.engine.summarizer)
        self.engine.summarizer.generate_silver.assert_not_called()
        result=resume_exclusion(self.slug,self.storage)
        self.assertEqual(result['content_status'],'invalidated')
        self.assertFalse((self.bronze/'transcript_raw.txt').exists())
        self.assertTrue((self.bronze/ARCHIVE/result['exclusion_id']/'audio').exists())

    def test_interrupted_local_exclusion_restores_without_remote_request(self):
        self.receipts()
        from castanha import recording_exclusion as module
        real_unlink=os.unlink
        def interrupted(name,*args,**kwargs):
            if name=='transcript_raw.txt':raise OSError('fixture interruption')
            return real_unlink(name,*args,**kwargs)
        with patch.object(module.os,'unlink',side_effect=interrupted):
            with self.assertRaises(OSError):self.exclude()
        identity=json.loads((self.bronze/POINTER).read_text())['id']
        with patch('castanha.zinom_adapter.ZinomMcpClient') as client:
            restore_recording(self.slug,identity,self.storage)
            client.assert_not_called()
        self.assertTrue((self.bronze/'capture_job1.ogg').exists())
        self.assertIn('CONTEUDO 1',self.storage.read_transcript(self.slug))

    def test_interrupted_restore_resumes_and_never_sends_forget(self):
        from castanha import recording_exclusion as module
        self.receipts();result=self.exclude()
        real_write=module._write_json
        def interrupted(fd,name,value):
            if name=='metadata.json' and value.get('recording_revision')==2:raise OSError('fixture interrupted restore')
            return real_write(fd,name,value)
        with patch.object(module,'_write_json',side_effect=interrupted):
            with self.assertRaises(OSError):restore_recording(self.slug,result['exclusion_id'],self.storage)
        with patch('castanha.zinom_adapter.ZinomMcpClient') as client:
            with self.assertRaises(ExclusionError):sync_meeting(self.slug,self.storage)
            restore_recording(self.slug,result['exclusion_id'],self.storage)
            client.assert_not_called()
        self.assertTrue((self.bronze/'capture_job1.ogg').exists())
        self.assertIn('CONTEUDO 1',self.storage.read_transcript(self.slug))

    def test_removed_job_copy_cannot_reenter_pipeline(self):
        job=(self.bronze/'.jobs/job1.json').read_bytes()
        self.exclude();(self.bronze/'.jobs/job1.json').write_bytes(job)
        self.engine.reprocess_meeting(self.slug)
        self.assertEqual(self.storage.read_transcript(self.slug),'CONTEUDO 2')
        self.assertEqual([r['job_id'] for r in self.storage._read_bronze_metadata(self.slug)['recordings']],['job2'])

    def test_capture_guard_and_source_change_block_before_mutation(self):
        self.engine.state_mgr.write({'status':'recording','capture_slug':self.slug})
        with self.assertRaises(ExclusionError):self.exclude()
        self.assertTrue((self.bronze/'capture_job1.ogg').exists())
        self.assertFalse((self.bronze/POINTER).exists())

    def test_capture_start_and_exclusion_share_nonblocking_mutation_gate(self):
        import fcntl
        from castanha.capture_gate import open_lock
        from castanha.config import get_state_dir
        with open_lock(get_state_dir()) as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            result=self.exclude()
            self.assertEqual(result['status'],'error')
            self.assertFalse((self.bronze/POINTER).exists())
            self.assertTrue((self.bronze/'capture_job1.ogg').exists())

    def test_crash_after_pointer_before_metadata_blocks_old_synthesis_and_resumes(self):
        from castanha import recording_exclusion as module
        from castanha.annotations import regenerate_annotations, AnnotationError
        original=module._write_json
        def interrupted(fd,name,value):
            if name=='metadata.json' and value.get('content_status')=='invalidated':
                raise OSError('fixture before first metadata write')
            return original(fd,name,value)
        with patch.object(module,'_write_json',side_effect=interrupted):
            with self.assertRaises(OSError):self.exclude()
        self.assertEqual(self.storage.get_meeting(self.slug)['recording_revision'],0)
        self.assertTrue((self.bronze/'transcript_raw.txt').exists())
        self.assertTrue(module.applicable(self.slug,self.storage))
        self.assertIn(('',self.slug),pending_candidates(self.storage))
        with self.assertRaises(AnnotationError):
            regenerate_annotations(self.slug,self.storage,summarizer=self.engine.summarizer)
        self.engine.summarizer.generate_silver.assert_not_called()
        result=sync_meeting(self.slug,self.storage)
        self.assertEqual(result['content_status'],'invalidated')
        self.assertFalse((self.bronze/'transcript_raw.txt').exists())
        self.assertFalse((self.bronze/'capture_job1.ogg').exists())

    def test_cli_retry_empty_is_valid_and_never_invokes_providers(self):
        self.one_audio();self.exclude()
        cli=str(Path(__file__).resolve().parents[1]/'bin/castanha')
        result=subprocess.run([cli,'retry',self.slug,'--json'],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['results'][0]['status'],'empty')
        self.assertFalse((self.bronze/'transcript_raw.txt').exists())
        self.assertFalse((self.storage.silver_dir/(self.slug+'.md')).exists())

    def test_cli_exclude_and_restore_use_same_meeting_and_revision(self):
        cli=str(Path(__file__).resolve().parents[1]/'bin/castanha')
        result=subprocess.run([cli,'recordings','exclude',self.slug,'capture_job1.ogg','--expected-revision','0','--json'],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        payload=json.loads(result.stdout)
        restored=subprocess.run([cli,'recordings','restore',self.slug,payload['exclusion_id'],'--json'],capture_output=True,text=True,timeout=10)
        self.assertEqual(restored.returncode,0,restored.stderr)
        self.assertTrue((self.bronze/'capture_job1.ogg').exists())


if __name__=='__main__':unittest.main()
