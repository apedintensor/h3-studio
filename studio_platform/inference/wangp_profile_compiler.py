"""Explicit native-profile compiler; legacy accepted BF16 recipes stay separate."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re

from .protocol import BackendError
from .wangp_contract import InputDescriptor, PreparedRequest, canonical_json
from .wangp_compiler import FIXED_CONTROLS, control_schema as legacy_schema
from ..runtime_catalog import get_profile, model_for, supported_cases, validate_manifest
from ..storage import key_belongs_to, validate_key


def control_schema(profile_id, mode):
    model_for(profile_id, mode)
    cases = [c for c in supported_cases(profile_id) if c['mode'] == mode]
    schema = legacy_schema()
    schema['steps'].update(default=20, enum=sorted({c['steps'] for c in cases}))
    schema['duration'].update(default=5, minimum=5, maximum=5, enum=[5])
    schema['resolution'].update(default='480P', enum=sorted({str(c['height'])+'P' for c in cases}))
    schema['aspect_ratio'].update(default='16:9', enum=['16:9'])
    for key in ('width', 'height'):
        values = sorted({c[key] for c in cases})
        schema[key].update(minimum=min(values), maximum=max(values), enum=values)
    # Marginal enums describe controls, not an unrestricted Cartesian product.
    # normalize_request also enforces the exact joint case including input roles.
    return schema


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def normalize_request(request, metadata, output_spec, profile_id):
    profile = get_profile(profile_id)
    if not isinstance(request, dict) or not isinstance(metadata, dict) or not isinstance(output_spec, dict):
        raise ValueError('wangp_invalid_request')
    mode = request.get('mode')
    model_for(profile_id, mode)
    if request.get('model') != profile['model_id']:
        raise ValueError('wangp_profile_model_mismatch')
    if set(request) - (set(control_schema(profile_id, mode)) | {'backend','model','mode','prompt','inputs','video_audio'}):
        raise ValueError('wangp_unsupported_controls')
    value = copy.deepcopy(request)
    value['backend'] = 'wangp-local'
    if not isinstance(value.get('prompt'), str) or not value['prompt'].strip() or len(value['prompt']) > 12000:
        raise ValueError('wangp_invalid_prompt')
    for key, expected in FIXED_CONTROLS.items():
        if key == 'steps':
            continue
        actual = value.get(key, expected)
        if key in {'shift_video','shift_audio'} and actual is None:
            actual = expected
        if actual != expected or isinstance(actual, bool) != isinstance(expected, bool):
            raise ValueError('wangp_unsupported_' + key)
        value[key] = expected
    steps = value.setdefault('steps', 20)
    if type(steps) is not int or steps not in (20, 50):
        raise ValueError('wangp_unsupported_steps')
    seed = value.get('seed', '0')
    if (isinstance(seed, bool) or not isinstance(seed, (str,int))
            or not re.fullmatch(r'[0-9]{1,20}', str(seed)) or int(seed) > 0xffffffffffffffff):
        raise ValueError('wangp_invalid_seed')
    value['seed'] = str(int(seed))
    if type(value.setdefault('duration', 5)) is not int or value['duration'] != 5:
        raise ValueError('wangp_profile_native_duration_required')
    value.setdefault('resolution', '480P')
    value.setdefault('aspect_ratio', '16:9')
    if value['resolution'] not in ('480P','768P') or value['aspect_ratio'] != '16:9':
        raise ValueError('wangp_profile_output_exceeded')
    from comfy_workflow import native_output_spec
    if (native_output_spec(value) != output_spec or output_spec.get('frames') != 124
            or any(type(output_spec.get(k)) is not int for k in ('width','height','frames'))):
        raise ValueError('wangp_output_spec_mismatch')
    for key in ('width','height'):
        if key in value and (type(value[key]) is not int or value[key] != output_spec[key]):
            raise ValueError('wangp_output_spec_mismatch')
    inputs = value.get('inputs', {})
    if not isinstance(inputs, dict) or set(inputs)-{'images','videos','audios','first_frame','last_frame'}:
        raise ValueError('wangp_invalid_inputs')
    inputs = {**{'images':[], 'videos':[], 'audios':[], 'first_frame':None, 'last_frame':None}, **inputs}
    ids, roles = [], []
    if mode == 'fl':
        if any(inputs[k] != [] for k in ('images','videos','audios')) or value.get('video_audio', {}) != {}:
            raise ValueError('wangp_first_last_images_only')
        pairs = [(role, 'image', [inputs[role]] if inputs[role] is not None else [])
                 for role in ('first_frame','last_frame')]
    else:
        if inputs['first_frame'] is not None or inputs['last_frame'] is not None:
            raise ValueError('wangp_ref_first_last_or_guides_unsupported')
        pairs = [(kind, kind, inputs[plural]) for plural,kind in
                 (('images','image'),('videos','video'),('audios','audio'))]
    for role, kind, values in pairs:
        if not isinstance(values, list) or len(values) > 1:
            raise ValueError('wangp_profile_reference_count_exceeded')
        for identity in values:
            if not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,200}', identity):
                raise ValueError('wangp_invalid_input_identity')
            meta = metadata.get(identity)
            if not isinstance(meta, dict) or meta.get('kind') != kind:
                raise ValueError('wangp_input_kind_mismatch')
            if mode == 'ref':
                if meta.get('model_ready') is not True:
                    raise ValueError('wangp_ref_media_not_ready')
                if kind in ('image','video'):
                    w,h = meta.get('width'),meta.get('height')
                    if (type(w) is not int or type(h) is not int or min(w,h)<256
                            or max(w,h)>832 or w*h>832*480 or not .4 <= w/h <= 2.5):
                        raise ValueError('wangp_ref_qualification_pixels_exceeded')
                if kind == 'video' and (meta.get('has_audio') is not False or meta.get('fps') != 24
                        or type(meta.get('frame_count')) is not int or meta['frame_count'] != 56
                        or not _number(meta.get('duration')) or abs(meta['duration']-56/24)>1e-6
                        or not _number(meta.get('source_duration')) or not 2 <= meta['source_duration'] <= 3.1):
                    raise ValueError('wangp_ref_aligned_video_required')
                if kind == 'audio' and (not _number(meta.get('duration')) or not 2 <= meta['duration'] <= 5.2
                        or meta.get('sample_rate') != 32000 or meta.get('channels') != 2):
                    raise ValueError('wangp_ref_selected_audio_required')
            ids.append(identity)
            roles.append(role)
    if len(set(ids)) != len(ids) or set(ids) != set(metadata):
        raise ValueError('wangp_input_snapshot_mismatch')
    expected_audio = {identity:False for identity in inputs['videos']}
    if (value.get('video_audio', {}) != expected_audio
            or any(type(v) is not bool for v in value.get('video_audio', {}).values())):
        raise ValueError('wangp_ref_soundtrack_unsupported')
    wanted = (mode, output_spec['width'], output_spec['height'], 124, 24, steps, sorted(roles))
    if not any((case['mode'], case['width'], case['height'], case['frames'], case['fps'],
                case['steps'], sorted(case['input_roles'])) == wanted for case in supported_cases(profile_id)):
        raise ValueError('wangp_profile_joint_envelope_unverified')
    value.update(inputs=inputs, video_audio=expected_audio)
    return value


def compile_settings(request, metadata, output_spec, handles, profile_id):
    value = normalize_request(request, metadata, output_spec, profile_id)
    if set(handles) != set(metadata):
        raise ValueError('wangp_input_handles_mismatch')
    runtime = get_profile(profile_id)['runtime']
    inputs = value['inputs']
    first, last = inputs['first_frame'], inputs['last_frame']
    images, videos, audios = (inputs[k] for k in ('images','videos','audios'))
    return {
        'model_type':model_for(profile_id, value['mode'])['model_type'], 'config':runtime['task_config'], 'image_mode':0,
        'prompt':value['prompt'], 'negative_prompt':'', 'alt_prompt':'',
        'resolution':f"{output_spec['width']}x{output_spec['height']}", 'video_length':124, 'force_fps':'24',
        'num_inference_steps':value['steps'], 'seed':int(value['seed']), 'guidance_scale':1.0,
        'guidance_phases':1, 'flow_shift':12.0, 'sample_solver':'euler', 'denoising_strength':1.0,
        'image_prompt_type':('S' if first else 'T')+('E' if last else ''),
        'image_start':handles.get(first), 'image_end':handles.get(last),
        'video_prompt_type':('I' if images else '')+('V-U' if videos else ''),
        'image_refs':[handles[i] for i in images] or None,
        'video_guide':handles[videos[0]] if videos else None,
        'audio_guide':handles[audios[0]] if audios else None,
        'audio_prompt_type':'A' if audios else '', 'image_refs_relative_size':100,
        'remove_background_images_ref':0, 'video_source':None, 'audio_source':None,
        'video_guide2':None, 'video_guide3':None, 'audio_guide2':None, 'audio_guide3':None,
        'repeat_generation':1, 'batch_size':1, 'multi_prompts_gen_type':'FG', 'multi_images_gen_type':0,
        'prompt_enhancer':'', 'activated_loras':[], 'skip_steps_cache_type':'',
        'override_attention':'sdpa', 'override_profile':runtime['memory_profile'],
        'guidance2_scale':1.0, 'guidance3_scale':1.0,
        'sliding_window_size':362, 'sliding_window_overlap':18,
        'sliding_window_discard_last_frames':0, 'sliding_window_trim_first_frames':0,
        'temporal_upsampling':'', 'spatial_upsampling':'', 'postprocess_audio':'',
        'custom_settings':{'audio_refinement':'none'},
        '_api':{'return_audio':True,'return_video_uint8':False,'return_side_files':False},
    }


class H3ProfileCompiler:
    def __init__(self, manifest, stage_input):
        validate_manifest(manifest)
        self.manifest, self.stage_input = manifest, stage_input

    def __call__(self, job, tag, store, heartbeat):
        try:
            doc = self.manifest.document
            identity, mode = doc['deployment_profile_id'], doc['mode']
            compiled, plan = job['request'], job['execution_plan']
            assets = compiled.get('assets', {})
            metadata = {key: val['metadata'] for key,val in assets.items()}
            request, output = compiled['request'], compiled['output_spec']
            normalize_request(request, metadata, output, identity)
            if (request['mode'] != mode or compiled.get('deployment_profile_id') != identity
                    or plan.get('deployment_profile_id') != identity
                    or compiled.get('recipe_id') != doc['generation_recipe_id']
                    or plan.get('engine_manifest_digest') != self.manifest.digest):
                raise ValueError('wangp_manifest_binding_mismatch')
            descriptors, handles, keys = [], {}, {}
            # Complete owner/hash checks before any transfer; same accepted-job ledger.
            for asset_id,snapshot in assets.items():
                model = snapshot['model']
                key = validate_key(model['key'])
                if not key_belongs_to(key, job['owner_id']):
                    raise ValueError('wangp_asset_owner_mismatch')
                sha, size = model['sha256'],model['size_bytes']
                handle = 'input-'+hashlib.sha256((tag+'\0'+asset_id+'\0'+sha).encode()).hexdigest()
                descriptors.append(InputDescriptor(asset_id,handle,metadata[asset_id]['kind'],sha,size))
                handles[asset_id], keys[asset_id] = handle,key
            settings = compile_settings(request,metadata,output,handles,identity)
            settings['output_filename'] = 'sixnine-'+tag
            prepared = PreparedRequest(job['id'],tag,job['request_hash'],self.manifest.digest,
                canonical_json(settings),canonical_json(output),True,tuple(descriptors))
            for item in descriptors:
                heartbeat()
                with store.open(keys[item.asset_id]) as source:
                    if self.stage_input(item,source,heartbeat=heartbeat) != item:
                        raise ValueError('wangp_staged_input_mismatch')
            return prepared
        except (KeyError,TypeError,ValueError) as error:
            code = str(error)
            if not re.fullmatch(r'wangp_[a-z0-9_]+',code):
                code = 'wangp_invalid_compiled_request'
            raise BackendError(code) from None


def validate_prepared(prepared, manifest):
    """Host boundary checks every setting before resolving any media path."""
    profile = validate_manifest(manifest)
    doc, settings = manifest.document, prepared.settings
    if prepared.manifest_digest != manifest.digest or not prepared.generate_audio:
        raise ValueError('wangp_manifest_binding_mismatch')
    descriptors = {d.handle:d for d in prepared.inputs}
    if len(descriptors) != len(prepared.inputs):
        raise ValueError('wangp_duplicate_input_handle')
    inputs = {'images':[], 'videos':[], 'audios':[], 'first_frame':None, 'last_frame':None}
    metadata, handles = {}, {}
    def take(handle, kind):
        if handle not in descriptors or descriptors[handle].kind != kind:
            raise ValueError('wangp_unbound_input_handle')
        d = descriptors[handle]
        if d.asset_id in handles:
            raise ValueError('wangp_duplicate_input_handle')
        handles[d.asset_id] = handle
        # These placeholders only reconstruct the control template. StagedInputs
        # independently probes the actual hash-bound bytes before dispatch.
        metadata[d.asset_id] = {'kind':kind,'model_ready':True,'width':832,'height':480,
            'frame_count':56,'fps':24,'duration':56/24 if kind=='video' else 5,
            'source_duration':56/24,'has_audio':False,'sample_rate':32000,'channels':2}
        return d.asset_id
    for role in ('first_frame','last_frame'):
        handle = settings.get('image_start' if role=='first_frame' else 'image_end')
        if handle is not None:
            inputs[role] = take(handle,'image')
    refs = settings.get('image_refs')
    if refs is not None:
        if not isinstance(refs,list) or len(refs)!=1:
            raise ValueError('wangp_profile_reference_count_exceeded')
        inputs['images'] = [take(refs[0],'image')]
    for field,plural,kind in (('video_guide','videos','video'),('audio_guide','audios','audio')):
        if settings.get(field) is not None:
            inputs[plural] = [take(settings[field],kind)]
    if len(handles) != len(descriptors):
        raise ValueError('wangp_unused_input_handle')
    output = json.loads(prepared.output_spec_json)
    request = {'model':profile['model_id'],'mode':doc['mode'],'prompt':settings.get('prompt'),
        'seed':settings.get('seed'),'steps':settings.get('num_inference_steps'),
        'resolution':str(output.get('height'))+'P','duration':5,'aspect_ratio':'16:9','inputs':inputs,
        'video_audio':{identity:False for identity in inputs['videos']}}
    expected = compile_settings(request,metadata,output,handles,profile['id'])
    expected['output_filename'] = 'sixnine-'+prepared.attempt_tag
    if canonical_json(expected) != canonical_json(settings):
        raise ValueError('wangp_profile_settings_mismatch')
    return profile
