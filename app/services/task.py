import math
import os.path
import re
import time
from os import path

from loguru import logger

from app.config import config
from app.models import const
from app.models.schema import VideoConcatMode, VideoParams
from app.services import llm, material, subtitle, video, voice, upload_post
from app.services import state as sm
from app.utils import utils


def generate_script(task_id, params):
    logger.info("\n\n## generating video script")
    video_script = params.video_script.strip()
    if not video_script:
        video_script = llm.generate_script(
            video_subject=params.video_subject,
            language=params.video_language,
            paragraph_number=params.paragraph_number,
            video_script_prompt=params.video_script_prompt,
            custom_system_prompt=params.custom_system_prompt,
        )
    else:
        logger.debug(f"video script: \n{video_script}")

    if not video_script:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        logger.error("failed to generate video script.")
        return None

    return video_script


def generate_terms(task_id, params, video_script):
    logger.info("\n\n## generating video terms")
    video_terms = params.video_terms
    if not video_terms:
        video_terms = llm.generate_terms(
            video_subject=params.video_subject, video_script=video_script, amount=5
        )
    else:
        if isinstance(video_terms, str):
            video_terms = [term.strip() for term in re.split(r"[,，]", video_terms)]
        elif isinstance(video_terms, list):
            video_terms = [term.strip() for term in video_terms]
        else:
            raise ValueError("video_terms must be a string or a list of strings.")

        logger.debug(f"video terms: {utils.to_json(video_terms)}")

    if not video_terms:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        logger.error("failed to generate video terms.")
        return None

    return video_terms


def save_script_data(task_id, video_script, video_terms, params):
    script_file = path.join(utils.task_dir(task_id), "script.json")
    script_data = {
        "script": video_script,
        "search_terms": video_terms,
        "params": params,
    }

    with open(script_file, "w", encoding="utf-8") as f:
        f.write(utils.to_json(script_data))


def generate_audio(task_id, params, video_script):
    '''
    Generate audio for the video script.
    If a custom audio file is provided, it will be used directly.
    There will be no subtitle maker object returned in this case.
    Otherwise, TTS will be used to generate the audio.
    Returns:
        - audio_file: path to the generated or provided audio file
        - audio_duration: duration of the audio in seconds
        - sub_maker: subtitle maker object if TTS is used, None otherwise
    '''
    logger.info("\n\n## generating audio")
    # /audio 和 /subtitle 请求模型不包含 custom_audio_file，
    # 这里统一做兼容读取，避免直调接口时抛属性错误。
    custom_audio_file = getattr(params, "custom_audio_file", None)
    
    # 简单处理自定义音频文件 - 先回退到原来的简单逻辑
    if custom_audio_file and os.path.exists(custom_audio_file):
        logger.info(f"using custom audio file: {custom_audio_file}")
        audio_duration = voice.get_audio_duration(custom_audio_file)
        if audio_duration == 0:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error("failed to get audio duration from custom audio file.")
            return None, None, None
        return custom_audio_file, audio_duration, None
    
    # 使用 TTS
    logger.info("no custom audio file, using TTS to generate audio.")
    audio_file = path.join(utils.task_dir(task_id), "audio.mp3")
    sub_maker = voice.tts(
        text=video_script,
        voice_name=voice.parse_voice_name(params.voice_name),
        voice_rate=params.voice_rate,
        voice_file=audio_file,
    )
    if sub_maker is None:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        logger.error(
            """failed to generate audio:
1. check if the language of the voice matches the language of the video script.
2. check if the network is available. If you are in China, it is recommended to use a VPN and enable the global traffic mode.
            """.strip()
        )
        return None, None, None
    audio_duration = math.ceil(voice.get_audio_duration(sub_maker))
    if audio_duration == 0:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        logger.error("failed to get audio duration.")
        return None, None, None
    return audio_file, audio_duration, sub_maker

def generate_subtitle(task_id, params, video_script, sub_maker, audio_file):
    '''
    Generate subtitle for the video script.
    If subtitle generation is disabled, return empty string.
    Otherwise, generate subtitle:
        - if sub_maker is available, use edge
        - elif audio_file is available, use whisper
        - else, create a simple subtitle from video_script
    Returns:
        - subtitle_path: path to the generated subtitle file
    '''
    logger.info("\n\n## generating subtitle")
    if not params.subtitle_enabled or not video_script:
        return ""

    subtitle_path = path.join(utils.task_dir(task_id), "subtitle.srt")
    
    # 情况 1: 有 sub_maker，用 edge
    if sub_maker is not None:
        subtitle_provider = config.app.get("subtitle_provider", "edge").strip().lower()
        logger.info(f"\n\n## generating subtitle, provider: {subtitle_provider}")
        subtitle_fallback = False
        if subtitle_provider == "edge":
            voice.create_subtitle(
                text=video_script, sub_maker=sub_maker, subtitle_file=subtitle_path
            )
            if not os.path.exists(subtitle_path):
                subtitle_fallback = True
                logger.warning("subtitle file not found, fallback to whisper")
        
        if subtitle_provider == "whisper" or subtitle_fallback:
            if audio_file:
                subtitle.create(audio_file=audio_file, subtitle_file=subtitle_path)
                logger.info("\n\n## correcting subtitle")
                subtitle.correct(subtitle_file=subtitle_path, video_script=video_script)
    
    # 情况 2: 没有 sub_maker 但有 audio_file，用 whisper
    elif audio_file is not None:
        logger.info("\n\n## generating subtitle with whisper (audio file available)")
        subtitle.create(audio_file=audio_file, subtitle_file=subtitle_path)
        logger.info("\n\n## correcting subtitle")
        subtitle.correct(subtitle_file=subtitle_path, video_script=video_script)
    
    # 情况 3: 既没有 sub_maker 也没有 audio_file，创建简单字幕
    else:
        logger.info("\n\n## generating simple subtitle from video script")
        # 用足够长的时长（默认600秒=10分钟），确保字幕能在整个视频中显示
        create_simple_subtitle(video_script, subtitle_path, duration_seconds=600)
    
    subtitle_lines = subtitle.file_to_subtitles(subtitle_path)
    if not subtitle_lines:
        logger.warning(f"subtitle file is invalid: {subtitle_path}")
        return ""

    return subtitle_path


def create_simple_subtitle(text, subtitle_path, duration_seconds=30):
    '''
    Create a simple SRT subtitle file from a text string.
    The whole text will be shown as one subtitle for the full video duration.
    '''
    # 转换秒数为 SRT 时间格式
    def seconds_to_srt_time(seconds):
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        millisecs = int((seconds % 1) * 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millisecs:03d}"
    
    # 生成字幕，持续整个视频时长
    lines = []
    lines.append("1")
    lines.append(f"{seconds_to_srt_time(0)} --> {seconds_to_srt_time(duration_seconds)}")
    # 清理文本中的多余换行
    cleaned_text = text.replace('\n', ' ').strip()
    lines.append(cleaned_text)
    lines.append("")
    
    with open(subtitle_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    
    logger.info(f"Simple subtitle created: {subtitle_path}, duration: {duration_seconds}s")
    return subtitle_path


def get_video_materials(task_id, params, video_terms, audio_duration):
    if params.video_source == "local":
        logger.info("\n\n## preprocess local materials")
        logger.info(f"params.video_materials before preprocess: {params.video_materials}")
        try:
            materials = video.preprocess_video(
                materials=params.video_materials, clip_duration=params.video_clip_duration
            )
            logger.info(f"Materials after preprocess: {materials}")
            if not materials:
                sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
                logger.error(
                    "no valid materials found, please check the materials and try again."
                )
                return None, None
            # 对于本地素材，返回完整的 materials 列表而不是只返回 URL
            return materials, "local_materials"
        except Exception as e:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error(f"Error in preprocess_video: {str(e)}", exc_info=True)
            return None, None
    elif params.video_source == "solid_color":
        logger.info("\n\n## creating solid color background")
        # 创建纯色背景视频
        bg_color = getattr(params, "solid_bg_color", "#667eea")
        # 解析颜色
        if bg_color.startswith('#'):
            bg_color = bg_color[1:]
            if len(bg_color) == 3:
                bg_color = ''.join([c * 2 for c in bg_color])
            bg_color = tuple(int(bg_color[i:i+2], 16) for i in (0, 2, 4))
        
        # 创建纯色背景视频
        solid_videos = video.create_solid_color_videos(
            task_id=task_id,
            bg_color=bg_color,
            duration=params.video_clip_duration,
            audio_duration=audio_duration * params.video_count,
            video_aspect=params.video_aspect
        )
        
        if not solid_videos:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error("failed to create solid color videos.")
            return None, None
        return solid_videos, "video_paths"
    else:
        logger.info(f"\n\n## downloading videos from {params.video_source}")
        downloaded_videos = material.download_videos(
            task_id=task_id,
            search_terms=video_terms,
            source=params.video_source,
            video_aspect=params.video_aspect,
            video_contact_mode=params.video_concat_mode,
            audio_duration=audio_duration * params.video_count,
            max_clip_duration=params.video_clip_duration,
        )
        if not downloaded_videos:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error(
                "failed to download videos, maybe the network is not available. if you are in China, please use a VPN."
            )
            return None, None
        return downloaded_videos, "video_paths"


def generate_final_videos(
    task_id, params, downloaded_videos, audio_file, subtitle_path, materials_type="video_paths"
):
    final_video_paths = []
    combined_video_paths = []
    video_concat_mode = (
        params.video_concat_mode if params.video_count == 1 else VideoConcatMode.random
    )
    video_transition_mode = params.video_transition_mode

    logger.info(f"Starting generate_final_videos, materials_type={materials_type}")
    if materials_type == "local_materials":
        logger.info(f"Number of local materials: {len(downloaded_videos)}")
        for i, material in enumerate(downloaded_videos):
            logger.info(f"Material {i+1}: url={material.url}, use_custom_clip={getattr(material, 'use_custom_clip', False)}, use_original_audio={getattr(material, 'use_original_audio', False)}")

    _progress = 50
    for i in range(params.video_count):
        index = i + 1
        combined_video_path = path.join(
            utils.task_dir(task_id), f"combined-{index}.mp4"
        )
        logger.info(f"\n\n## combining video: {index} => {combined_video_path}")
        
        # 检查是否有素材需要使用原音频
        use_original_audio = False
        if materials_type == "local_materials":
            for material in downloaded_videos:
                if getattr(material, "use_original_audio", False):
                    use_original_audio = True
                    logger.info(f"Found material with use_original_audio=True: {material.url}")
                    break
        
        try:
            if materials_type == "local_materials":
                # 对于本地素材，传递完整的 materials 列表
                # 如果使用原音频，不传 audio_file
                video.combine_videos_with_materials(
                    combined_video_path=combined_video_path,
                    video_materials=downloaded_videos,
                    audio_file=audio_file,
                    video_aspect=params.video_aspect,
                    video_concat_mode=video_concat_mode,
                    video_transition_mode=video_transition_mode,
                    max_clip_duration=params.video_clip_duration,
                    threads=params.n_threads,
                )
            else:
                # 原来的方式，传递视频路径列表
                video.combine_videos(
                    combined_video_path=combined_video_path,
                    video_paths=downloaded_videos,
                    audio_file=audio_file,
                    video_aspect=params.video_aspect,
                    video_concat_mode=video_concat_mode,
                    video_transition_mode=video_transition_mode,
                    max_clip_duration=params.video_clip_duration,
                    threads=params.n_threads,
                )
            
            # 检查 combined 视频是否生成成功
            if os.path.exists(combined_video_path):
                file_size = os.path.getsize(combined_video_path)
                logger.success(f"Combined video generated: {combined_video_path} ({file_size} bytes)")
            else:
                logger.error(f"Combined video not found: {combined_video_path}")
        except Exception as e:
            logger.error(f"Error combining videos: {str(e)}", exc_info=True)
            raise

        _progress += 50 / params.video_count / 2
        sm.state.update_task(task_id, progress=_progress)

        final_video_path = path.join(utils.task_dir(task_id), f"final-{index}.mp4")

        logger.info(f"\n\n## generating video: {index} => {final_video_path}")
        
        # 如果使用原音频，audio_path 传 None
        audio_path_to_use = audio_file
        
        try:
            video.generate_video(
                video_path=combined_video_path,
                audio_path=audio_path_to_use,
                subtitle_path=subtitle_path,
                output_file=final_video_path,
                params=params,
            )
            
            # 检查 final 视频是否生成成功
            if os.path.exists(final_video_path):
                file_size = os.path.getsize(final_video_path)
                logger.success(f"Final video generated: {final_video_path} ({file_size} bytes)")
            else:
                logger.error(f"Final video not found: {final_video_path}")
        except Exception as e:
            logger.error(f"Error generating video: {str(e)}", exc_info=True)
            raise

        _progress += 50 / params.video_count / 2
        sm.state.update_task(task_id, progress=_progress)

        final_video_paths.append(final_video_path)
        combined_video_paths.append(combined_video_path)

    logger.info(f"Generated {len(final_video_paths)} final videos: {final_video_paths}")
    return final_video_paths, combined_video_paths


def start(task_id, params: VideoParams, stop_at: str = "video"):
    import time
    total_start_time = time.time()
    logger.info("="*60)
    logger.info(f"🚀 START TASK: {task_id}, stop_at: {stop_at}")
    logger.info("="*60)
    logger.info(f"params.custom_audio_file: {getattr(params, 'custom_audio_file', None)}")
    logger.info(f"params.video_source: {params.video_source}")
    logger.info(f"params.video_materials: {params.video_materials}")
    logger.info(f"params.voice_name: {params.voice_name}")
    logger.info(f"params.bgm_type: {params.bgm_type}")
    
    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=5)

    # 1. Generate script
    stage_start_time = time.time()
    logger.info("\n" + "="*60)
    logger.info("📝 STAGE 1: Generate Script")
    logger.info("="*60)
    try:
        video_script = generate_script(task_id, params)
        if not video_script or "Error: " in video_script:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error(f"Failed to generate script: {video_script}")
            return
        logger.success(f"✅ Script generated successfully ({time.time() - stage_start_time:.2f}s)")
        logger.info(f"Generated script: {video_script}")
    except Exception as e:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        logger.error(f"❌ Error generating script: {str(e)}", exc_info=True)
        return

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=10)

    if stop_at == "script":
        sm.state.update_task(
            task_id, state=const.TASK_STATE_COMPLETE, progress=100, script=video_script
        )
        return {"script": video_script}

    # 2. Generate terms
    stage_start_time = time.time()
    logger.info("\n" + "="*60)
    logger.info("🔍 STAGE 2: Generate Terms")
    logger.info("="*60)
    video_terms = ""
    if params.video_source != "local":
        video_terms = generate_terms(task_id, params, video_script)
        if not video_terms:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            return
        logger.success(f"✅ Terms generated successfully ({time.time() - stage_start_time:.2f}s)")
    else:
        logger.info("Skipping terms generation (using local materials)")

    save_script_data(task_id, video_script, video_terms, params)

    if stop_at == "terms":
        sm.state.update_task(
            task_id, state=const.TASK_STATE_COMPLETE, progress=100, terms=video_terms
        )
        return {"script": video_script, "terms": video_terms}

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=20)

    # 3. Generate audio (if voice_name is provided)
    stage_start_time = time.time()
    logger.info("\n" + "="*60)
    logger.info("🎙️ STAGE 3: Generate Audio")
    logger.info("="*60)
    audio_file = None
    audio_duration = 0
    sub_maker = None
    
    # 检查是否有本地素材使用原音频，或者没有提供配音
    custom_audio_file = getattr(params, "custom_audio_file", None)
    should_generate_audio = bool(params.voice_name or custom_audio_file)

    # 如果有原音频素材，或者没有提供配音名称，就不生成配音
    if should_generate_audio:
        audio_file, audio_duration, sub_maker = generate_audio(
            task_id, params, video_script
        )
        if not audio_file:
            # 如果尝试生成配音但失败了，仍然继续（使用素材音频）
            logger.warning("Failed to generate audio, will use original material audio if available")
            audio_file = None
            audio_duration = 0
            sub_maker = None
        else:
            logger.success(f"✅ Audio generated successfully ({time.time() - stage_start_time:.2f}s)")
            logger.info(f"Audio duration: {audio_duration:.2f}s")
    else:
        logger.info("Skipping audio generation (no voice-over selected)")

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=30)

    if stop_at == "audio":
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            audio_file=audio_file,
        )
        return {"audio_file": audio_file, "audio_duration": audio_duration}

    # 4. Generate subtitle
    stage_start_time = time.time()
    logger.info("\n" + "="*60)
    logger.info("📄 STAGE 4: Generate Subtitle")
    logger.info("="*60)
    subtitle_path = None
    if video_script and params.subtitle_enabled:
        # 即使没有 audio_file，只要有 video_script 且启用了字幕，就尝试生成
        # 如果没有 audio_file 或 sub_maker，我们用视频脚本来创建一个简单字幕
        subtitle_path = generate_subtitle(
            task_id, params, video_script, sub_maker, audio_file
        )
        if subtitle_path:
            logger.success(f"✅ Subtitle generated successfully ({time.time() - stage_start_time:.2f}s)")
        else:
            logger.warning("Subtitle generation failed, but will continue without subtitles")
    else:
        logger.info("Skipping subtitle generation (subtitle disabled)")

    if stop_at == "subtitle":
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            subtitle_path=subtitle_path,
        )
        return {"subtitle_path": subtitle_path}

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=40)

    # 5. Get video materials
    stage_start_time = time.time()
    logger.info("\n" + "="*60)
    logger.info("🎬 STAGE 5: Get Video Materials")
    logger.info("="*60)
    try:
        downloaded_videos, materials_type = get_video_materials(
            task_id, params, video_terms, audio_duration
        )
        logger.info(f"downloaded_videos: {downloaded_videos}")
        logger.info(f"materials_type: {materials_type}")
        if not downloaded_videos:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error("No downloaded videos found")
            return
        logger.success(f"✅ Materials retrieved successfully ({time.time() - stage_start_time:.2f}s)")
        logger.info(f"Number of materials: {len(downloaded_videos)}")
    except Exception as e:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        logger.error(f"❌ Error getting video materials: {str(e)}", exc_info=True)
        return

    if stop_at == "materials":
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            materials=downloaded_videos,
        )
        return {"materials": downloaded_videos}

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=50)

    # 仅完整视频生成流程才需要处理视频拼接模式；
    # 这样可以避免 /subtitle 和 /audio 这类请求访问不存在的字段。
    if type(params.video_concat_mode) is str:
        params.video_concat_mode = VideoConcatMode(params.video_concat_mode)

    # 6. Generate final videos
    stage_start_time = time.time()
    logger.info("\n" + "="*60)
    logger.info("🎥 STAGE 6: Generate Final Videos")
    logger.info("="*60)
    try:
        final_video_paths, combined_video_paths = generate_final_videos(
            task_id, params, downloaded_videos, audio_file, subtitle_path, materials_type
        )

        if not final_video_paths:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error("No video files generated!")
            return
        logger.success(f"✅ Final videos generated successfully ({time.time() - stage_start_time:.2f}s)")
    except Exception as e:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        logger.error(f"❌ Error generating final videos: {str(e)}", exc_info=True)
        return

    logger.success(
        f"task {task_id} finished, generated {len(final_video_paths)} videos."
    )
    
    # 验证生成的视频文件
    for i, video_path in enumerate(final_video_paths):
        if os.path.exists(video_path):
            file_size = os.path.getsize(video_path)
            logger.success(f"Video {i+1} generated: {video_path} ({file_size} bytes)")
        else:
            logger.error(f"Video {i+1} NOT found: {video_path}")

    # 7. Cross-post to TikTok/Instagram (if enabled)
    cross_post_results = []
    if upload_post.upload_post_service.is_configured() and upload_post.upload_post_service.auto_upload:
        stage_start_time = time.time()
        logger.info("\n" + "="*60)
        logger.info("📱 STAGE 7: Cross-posting to TikTok/Instagram")
        logger.info("="*60)
        for video_path in final_video_paths:
            result = upload_post.cross_post_video(
                video_path=video_path,
                title=params.video_subject or "Check out this video! #shorts #viral"
            )
            cross_post_results.append(result)
            if result.get('success'):
                logger.info(f"✅ Cross-posted: {video_path}")
            else:
                logger.warning(f"⚠️ Failed to cross-post: {video_path} - {result.get('error', 'Unknown error')}")
        logger.success(f"Cross-posting completed ({time.time() - stage_start_time:.2f}s)")

    total_duration = time.time() - total_start_time
    logger.info("\n" + "="*60)
    logger.success(f"🎉 TASK COMPLETED IN {total_duration:.2f}s")
    logger.info("="*60)
    
    kwargs = {
        "videos": final_video_paths,
        "combined_videos": combined_video_paths,
        "script": video_script,
        "terms": video_terms,
        "audio_file": audio_file,
        "audio_duration": audio_duration,
        "subtitle_path": subtitle_path,
        "materials": downloaded_videos,
        "cross_post_results": cross_post_results if cross_post_results else None,
        "total_duration": total_duration,
    }
    sm.state.update_task(
        task_id, state=const.TASK_STATE_COMPLETE, progress=100, **kwargs
    )
    return kwargs


if __name__ == "__main__":
    task_id = "task_id"
    params = VideoParams(
        video_subject="金钱的作用",
        voice_name="zh-CN-XiaoyiNeural-Female",
        voice_rate=1.0,
    )
    start(task_id, params, stop_at="video")
