#!/usr/bin/env python3
"""Small local web frontend for the BBK 9588 QEMU system emulator."""

from __future__ import annotations

import argparse
import cProfile
import pstats
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from emu.qemu.system import (
    DEFAULT_QEMU_CPU,
    DEFAULT_QEMU_EXECUTABLE,
    DEFAULT_QEMU_MACHINE,
)
from emu.web.frontend_server import FrontendHandler as Handler
from emu.web.frontend_state import (
    DEFAULT_WEB_QEMU_ICOUNT,
    FrontendState,
    display_to_panel_point,
    display_to_raw_point,
    display_to_touch_point,
    raw_to_display_point,
)

HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>BBK 9588 HWEMU</title>
  <style>
    :root { color-scheme: dark; font-family: "Segoe UI", sans-serif; background: #15171a; color: #e8eaed; letter-spacing: 0; }
    * { box-sizing: border-box; }
    html, body { width: 100%; height: 100%; overflow: hidden; }
    body { margin: 0; background: #15171a; }
    .app-header { height: 56px; display: flex; align-items: center; gap: 10px; padding: 0 18px; border-bottom: 1px solid #343941; background: #191b1f; }
    .workspace { height: calc(100vh - 56px); min-height: 0; display: grid; grid-template-columns: minmax(280px, 340px) minmax(390px, 1fr) minmax(280px, 340px); grid-template-areas: "controls stage status"; overflow: hidden; }
    .control-sidebar { grid-area: controls; padding: 16px; border-right: 1px solid #343941; background: #1b1e22; overflow: auto; }
    .emulator-stage { grid-area: stage; min-width: 0; min-height: 0; padding: 16px 20px 18px; display: flex; flex-direction: column; align-items: center; gap: 12px; overflow: hidden; }
    .status-sidebar { grid-area: status; min-height: 0; padding: 16px; border-left: 1px solid #343941; background: #202327; display: flex; flex-direction: column; overflow: hidden; }
    h1 { font-size: 18px; margin: 0; font-weight: 650; }
    h2 { font-size: 13px; margin: 0 0 10px; color: #b8c0cc; font-weight: 600; }
    button, input, select { font: inherit; }
    button { background: #2f6fed; color: white; border: 0; border-radius: 6px; padding: 8px 10px; cursor: pointer; touch-action: manipulation; user-select: none; -webkit-user-select: none; -webkit-touch-callout: none; -webkit-tap-highlight-color: transparent; }
    button.secondary { background: #343941; }
    button.warn { background: #9b3b3b; }
    button:disabled { opacity: .55; cursor: default; }
    .icon-button { width: 40px; height: 36px; display: inline-grid; place-items: center; padding: 0; font-size: 22px; line-height: 1; }
    .power-button { position: relative; color: #d5dae1; }
    .power-button::before { content: ""; width: 17px; height: 17px; border: 2px solid currentColor; border-radius: 50%; }
    .power-button::after { content: ""; width: 2px; height: 11px; position: absolute; top: 7px; left: 19px; background: currentColor; box-shadow: 0 0 0 2px #343941; }
    .power-button.active { background: #9b3b3b; }
    .power-button.active::after { box-shadow: 0 0 0 2px #9b3b3b; }
    .audio-button { color: #d5dae1; }
    .audio-icon { position: relative; display: block; width: 24px; height: 20px; }
    .audio-speaker { position: absolute; left: 1px; top: 5px; width: 12px; height: 10px; background: currentColor; clip-path: polygon(0 30%, 32% 30%, 100% 0, 100% 100%, 32% 70%, 0 70%); }
    .audio-wave { position: absolute; border-right: 2px solid currentColor; border-radius: 0 50% 50% 0; }
    .audio-wave-one { right: 5px; top: 5px; width: 5px; height: 10px; }
    .audio-wave-two { right: 0; top: 2px; width: 8px; height: 16px; }
    .audio-slash { display: none; position: absolute; left: 16px; top: 1px; width: 2px; height: 18px; border-radius: 1px; background: currentColor; transform: rotate(-42deg); }
    .audio-button.is-muted .audio-wave { display: none; }
    .audio-button.is-muted .audio-slash { display: block; }
    .settings-button { color: #d5dae1; font-size: 20px; }
    .screen-toolbar { width: min(560px, 100%); position: relative; z-index: 2; flex: 0 0 auto; display: flex; justify-content: center; align-items: center; gap: 8px; }
    .orientation-label { min-width: 54px; text-align: center; color: #c9d1d9; font-size: 12px; font-variant-numeric: tabular-nums; }
    .screen-wrap { width: 100%; min-height: 0; position: relative; flex: 1 1 auto; display: flex; justify-content: center; align-items: center; overflow: hidden; background: #08090b; border: 1px solid #343941; border-radius: 8px; padding: 12px; }
    #screen { display: block; width: auto; height: auto; max-width: 100%; max-height: 100%; image-rendering: pixelated; background: #000; cursor: crosshair; touch-action: none; user-select: none; -webkit-user-select: none; -webkit-touch-callout: none; }
    .screen-stop-overlay { position: absolute; inset: 12px; z-index: 1; display: flex; flex-direction: column; justify-content: center; align-items: center; gap: 9px; padding: 24px; background: rgba(8, 9, 11, .92); text-align: center; }
    .screen-stop-overlay[hidden] { display: none; }
    .screen-stop-icon { width: 32px; height: 32px; position: relative; color: #b8c0cc; }
    .screen-stop-icon::before { content: ""; position: absolute; inset: 5px 3px 1px; border: 2px solid currentColor; border-radius: 50%; }
    .screen-stop-icon::after { content: ""; position: absolute; top: 0; left: 14px; width: 4px; height: 17px; border-left: 2px solid currentColor; background: rgba(8, 9, 11, .92); }
    .screen-stop-title { font-size: 17px; font-weight: 650; }
    .screen-stop-message { max-width: 320px; color: #aeb7c4; font-size: 12px; line-height: 1.5; overflow-wrap: anywhere; }
    .screen-stop-overlay.error .screen-stop-icon, .screen-stop-overlay.error .screen-stop-title { color: #ff9f9f; }
    .screen-stop-overlay button { margin-top: 5px; }
    .fullscreen-exit { display: none; position: absolute; top: 12px; right: 12px; z-index: 2; background: rgba(32, 35, 39, .86); }
    .screen-wrap:fullscreen, .screen-wrap:-webkit-full-screen { width: 100vw; height: 100vh; padding: 16px; border: 0; border-radius: 0; background: #000; position: relative; }
    .screen-wrap:fullscreen #screen, .screen-wrap:-webkit-full-screen #screen { width: auto; height: auto; max-width: none; max-height: none; }
    .screen-wrap:fullscreen .screen-stop-overlay, .screen-wrap:-webkit-full-screen .screen-stop-overlay { inset: 16px; }
    .screen-wrap:fullscreen .fullscreen-exit, .screen-wrap:-webkit-full-screen .fullscreen-exit { display: grid; }
    .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .panel { border: 1px solid #343941; border-radius: 8px; padding: 12px; background: #1b1e22; margin-bottom: 14px; }
    .kv { display: grid; grid-template-columns: minmax(100px, 120px) minmax(0, 1fr); gap: 5px 10px; font-size: 12px; }
    .kv > div { min-width: 0; }
    .kv-value { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .device-controls { width: min(560px, 100%); display: flex; flex-direction: column; align-items: center; gap: 14px; }
    .device-keypad { display: grid; grid-template-columns: repeat(5, 54px); grid-template-rows: repeat(2, 48px); gap: 8px; justify-content: center; }
    .device-key { min-width: 0; min-height: 0; padding: 5px; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 2px; background: #343941; touch-action: none; }
    .device-key > * { pointer-events: none; user-select: none; -webkit-user-select: none; }
    .device-key.active { background: #5794ff; }
    .device-key .key-symbol { font-size: 19px; line-height: 1; }
    .device-key kbd { min-width: 24px; color: #adb7c5; font-size: 10px; font-family: inherit; font-weight: 500; }
    .device-key.active kbd { color: white; }
    .key-up { grid-column: 3; grid-row: 1; }
    .key-left { grid-column: 2; grid-row: 2; }
    .key-down { grid-column: 3; grid-row: 2; }
    .key-right { grid-column: 4; grid-row: 2; }
    .key-cancel { grid-column: 1; grid-row: 1 / 3; }
    .key-ok { grid-column: 5; grid-row: 1 / 3; }
    .settings-dialog { width: min(620px, calc(100vw - 24px)); max-width: none; max-height: calc(100dvh - 24px); padding: 0; border: 1px solid #414751; border-radius: 8px; background: #1b1e22; color: #e8eaed; box-shadow: 0 18px 50px rgba(0, 0, 0, .55); }
    .settings-dialog::backdrop { background: rgba(0, 0, 0, .66); }
    .settings-dialog-shell { max-height: calc(100dvh - 26px); display: flex; flex-direction: column; overflow: hidden; }
    .settings-dialog-header { min-height: 52px; display: flex; align-items: center; justify-content: space-between; padding: 10px 12px 10px 16px; border-bottom: 1px solid #343941; }
    .settings-dialog-header h2 { margin: 0; color: #e8eaed; font-size: 15px; }
    .settings-close { width: 32px; height: 32px; padding: 0; background: #343941; font-size: 22px; line-height: 1; }
    .settings-tabs { display: flex; gap: 4px; padding: 10px 14px 0; }
    .settings-tab { background: transparent; color: #aeb7c4; border-bottom: 2px solid transparent; border-radius: 4px 4px 0 0; padding: 8px 12px; }
    .settings-tab.active { color: white; border-bottom-color: #5794ff; background: #25292f; }
    .settings-dialog-body { min-height: 0; padding: 14px; overflow-y: auto; overscroll-behavior: contain; }
    .settings-pane[hidden] { display: none; }
    .setting-row { min-height: 46px; display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 4px 2px; }
    .setting-row .check { margin-left: auto; }
    .keymap-panel { width: 100%; }
    .keymap-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
    .keymap-header h2 { margin: 0; }
    .keymap-header .icon-button { width: 32px; height: 30px; font-size: 18px; }
    .binding-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 7px; }
    .binding-control { display: grid; grid-template-columns: 42px minmax(0, 1fr); align-items: center; gap: 6px; color: #b8c0cc; font-size: 12px; }
    .binding-control button { min-width: 0; height: 32px; padding: 4px 6px; background: #2a2e34; border: 1px solid #414751; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .binding-control button.capturing { border-color: #5794ff; background: #253c62; }
    .gamepad-status { min-height: 18px; margin-top: 8px; color: #9aa4b2; font-size: 12px; text-align: center; overflow-wrap: anywhere; }
    .gamepad-status.error { color: #ff9f9f; }
    input, select { color: #e8eaed; background: #111317; border: 1px solid #3b414b; border-radius: 6px; padding: 7px; }
    input { width: 90px; }
    input[type="checkbox"] { width: auto; accent-color: #5794ff; }
    .grow { flex: 1 1 180px; min-width: 0; }
    .path-input { flex: 1 1 260px; width: auto; min-width: 0; }
    .image-status { min-height: 1.2em; overflow-wrap: anywhere; }
    .check { display: inline-flex; gap: 6px; align-items: center; color: #c9d1d9; font-size: 12px; }
    .sidebar-tabs { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 4px; margin-bottom: 12px; padding: 3px; background: #181b1f; border: 1px solid #343941; border-radius: 7px; }
    .sidebar-tab { background: transparent; color: #aeb7c4; padding: 7px 8px; }
    .sidebar-tab.active { background: #343941; color: white; }
    .tab-pane { min-height: 0; margin-bottom: 0; }
    .tab-pane[hidden] { display: none; }
    #statusTab { flex: 1; overflow-y: auto; }
    #filesTab { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
    #filesTab[hidden] { display: none; }
    .file-toolbar { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 9px; }
    .file-toolbar button { min-width: 34px; padding: 6px 8px; }
    .file-path { min-height: 28px; padding: 6px 8px; margin-bottom: 8px; background: #111317; border: 1px solid #343941; border-radius: 5px; color: #cbd3dd; font-size: 12px; overflow-wrap: anywhere; }
    .file-list { flex: 1; min-height: 0; display: flex; flex-direction: column; border-top: 1px solid #343941; overflow-y: auto; overscroll-behavior: contain; scrollbar-gutter: stable; }
    .file-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 6px; align-items: center; padding: 7px 0; border-bottom: 1px solid #30343b; }
    .file-name { min-width: 0; display: flex; align-items: center; gap: 7px; padding: 3px 2px; color: #e8eaed; background: transparent; text-align: left; overflow: hidden; }
    .file-name-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .file-size { color: #8f99a7; font-size: 11px; white-space: nowrap; }
    .file-actions { display: flex; gap: 3px; }
    .file-actions button { width: 28px; height: 28px; padding: 0; background: #343941; }
    .file-actions button.delete { background: #693333; }
    .file-empty { padding: 20px 4px; color: #8f99a7; font-size: 12px; text-align: center; }
    .file-manager-status { min-height: 18px; margin-top: 8px; color: #9aa4b2; font-size: 12px; overflow-wrap: anywhere; }
    .operation-status { width: 100%; min-height: 18px; color: #9aa4b2; font-size: 12px; overflow-wrap: anywhere; }
    .operation-status.error { color: #ff9f9f; }
    .feedback-actions { margin-top: 8px; }
    .mobile-drawer-button, .drawer-header, .drawer-backdrop { display: none; }
    .drawer-header { min-height: 38px; align-items: center; justify-content: space-between; margin-bottom: 12px; color: #cbd3dd; font-size: 13px; font-weight: 600; }
    .drawer-close { width: 32px; height: 32px; padding: 0; background: #343941; font-size: 22px; line-height: 1; }
    pre { margin: 0; white-space: pre-wrap; word-break: break-word; font-size: 12px; line-height: 1.45; color: #c9d1d9; }
    .muted { color: #9aa4b2; font-size: 12px; }
    @media (max-width: 1120px) {
      .workspace { grid-template-columns: minmax(250px, 300px) minmax(390px, 1fr); grid-template-rows: minmax(0, 1fr) minmax(220px, 38vh); grid-template-areas: "controls stage" "status status"; }
      .status-sidebar { border-left: 0; border-top: 1px solid #343941; }
    }
    @media (max-width: 760px) {
      .app-header { height: 50px; justify-content: space-between; padding: 0 10px; }
      .app-header h1 { flex: 1; text-align: center; font-size: 16px; }
      .mobile-drawer-button { width: 36px; height: 34px; display: inline-grid; place-items: center; flex: 0 0 auto; padding: 0; background: #343941; font-size: 20px; }
      .workspace { height: calc(100dvh - 50px); display: block; overflow: hidden; }
      .emulator-stage { width: 100%; height: 100%; padding: 8px 10px 12px; gap: 8px; overflow: hidden; }
      .screen-toolbar { flex: 0 0 auto; }
      .screen-wrap { flex: 1 1 auto; min-height: 0; padding: 6px; }
      .device-controls { flex: 0 0 auto; gap: 0; }
      .control-sidebar, .status-sidebar {
        width: min(86vw, 340px); height: calc(100dvh - 50px); position: fixed; top: 50px; bottom: 0; z-index: 30;
        padding: 12px; border: 0; box-shadow: 0 0 24px rgba(0, 0, 0, .45); transition: transform 180ms ease;
      }
      .control-sidebar { left: 0; transform: translateX(-105%); }
      .status-sidebar { right: 0; transform: translateX(105%); }
      .control-sidebar.drawer-open, .status-sidebar.drawer-open { transform: translateX(0); }
      .drawer-header { display: flex; }
      .drawer-backdrop { position: fixed; inset: 50px 0 0; z-index: 25; width: 100%; height: auto; padding: 0; border: 0; border-radius: 0; background: rgba(0, 0, 0, .55); }
      .drawer-backdrop:not([hidden]) { display: block; }
    }
    @media (max-width: 560px) {
      .binding-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 380px) {
      .device-keypad { grid-template-columns: repeat(5, 48px); gap: 6px; }
      .binding-grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header class="app-header">
    <button id="openControlsDrawer" class="mobile-drawer-button" title="镜像与运行" aria-label="打开镜像与运行抽屉" aria-controls="controlsDrawer" aria-expanded="false">☰</button>
    <h1>BBK 9588 硬件仿真器</h1>
    <button id="openStatusDrawer" class="mobile-drawer-button" title="状态与文件" aria-label="打开状态与文件抽屉" aria-controls="statusDrawer" aria-expanded="false">☷</button>
  </header>
  <div class="workspace">
    <aside id="controlsDrawer" class="control-sidebar" aria-label="镜像与运行控制">
      <div class="drawer-header"><span>镜像与运行</span><button class="drawer-close" data-close-drawer title="关闭" aria-label="关闭镜像与运行抽屉">×</button></div>
      <section class="panel">
        <h2>NAND 镜像</h2>
        <div class="row">
          <div id="imageStatus" class="image-status grow">bbk9588_nand.bin</div>
        </div>
      </section>
      <section class="panel">
        <h2>模拟器</h2>
        <div class="row">
          <button id="stop" class="secondary">安全关机</button>
          <button id="reset" class="secondary">重新启动</button>
          <button id="forceStop" class="warn">强制停止</button>
        </div>
        <div class="row feedback-actions">
          <button id="saveFeedback" class="secondary" title="保存状态、日志和当前屏幕截图">保存反馈包</button>
        </div>
        <div id="operationStatus" class="operation-status" role="status" aria-live="polite"></div>
      </section>
    </aside>
    <main class="emulator-stage">
      <div class="screen-toolbar" role="toolbar" aria-label="屏幕控制">
        <button id="openSettings" class="secondary icon-button settings-button" title="设置" aria-label="打开设置" aria-haspopup="dialog" aria-controls="settingsDialog">⚙</button>
        <button id="rotateLeft" class="secondary icon-button" title="向左旋转 90°" aria-label="向左旋转 90°">↶</button>
        <span id="orientationLabel" class="orientation-label">180°</span>
        <button id="rotateRight" class="secondary icon-button" title="向右旋转 90°" aria-label="向右旋转 90°">↷</button>
        <button id="toggleAudio" class="secondary icon-button audio-button" title="关闭声音" aria-label="关闭声音">
          <span class="audio-icon" aria-hidden="true">
            <span class="audio-speaker"></span>
            <span class="audio-wave audio-wave-one"></span>
            <span class="audio-wave audio-wave-two"></span>
            <span class="audio-slash"></span>
          </span>
        </button>
        <button id="toggleFullscreen" class="secondary icon-button" title="全屏" aria-label="全屏">⛶</button>
        <button id="powerKey" class="secondary icon-button power-button" data-key="11" data-name="power" title="电源" aria-label="电源"></button>
      </div>
      <div id="screenWrap" class="screen-wrap">
        <canvas id="screen" width="240" height="320"></canvas>
        <div id="screenStopOverlay" class="screen-stop-overlay" role="status" aria-live="polite" hidden>
          <div class="screen-stop-icon" aria-hidden="true"></div>
          <div id="screenStopTitle" class="screen-stop-title">设备已关机</div>
          <div id="screenStopMessage" class="screen-stop-message"></div>
          <button id="restartFromOverlay">重新启动</button>
        </div>
        <button id="exitFullscreen" class="secondary icon-button fullscreen-exit" title="退出全屏" aria-label="退出全屏">×</button>
      </div>
      <div class="device-controls">
        <div class="device-keypad" aria-label="设备按键">
          <button class="device-key key-up" data-key="4" data-name="up" aria-label="上"><span class="key-symbol">↑</span><kbd data-key-hint="4">W</kbd></button>
          <button class="device-key key-left" data-key="6" data-name="left" aria-label="左"><span class="key-symbol">←</span><kbd data-key-hint="6">A</kbd></button>
          <button class="device-key key-down" data-key="5" data-name="down" aria-label="下"><span class="key-symbol">↓</span><kbd data-key-hint="5">S</kbd></button>
          <button class="device-key key-right" data-key="7" data-name="right" aria-label="右"><span class="key-symbol">→</span><kbd data-key-hint="7">D</kbd></button>
          <button class="device-key key-cancel" data-key="9" data-name="cancel" aria-label="退出"><span class="key-symbol">退出</span><kbd data-key-hint="9">K</kbd></button>
          <button class="device-key key-ok" data-key="10" data-name="ok" aria-label="确定"><span class="key-symbol">确定</span><kbd data-key-hint="10">J</kbd></button>
        </div>
      </div>
    </main>
    <aside id="statusDrawer" class="status-sidebar" aria-label="状态与文件">
      <div class="drawer-header"><span>状态与文件</span><button class="drawer-close" data-close-drawer title="关闭" aria-label="关闭状态与文件抽屉">×</button></div>
      <div class="sidebar-tabs" role="tablist" aria-label="右侧视图">
        <button id="statusTabButton" class="sidebar-tab active" role="tab" aria-selected="true" aria-controls="statusTab">状态</button>
        <button id="filesTabButton" class="sidebar-tab" role="tab" aria-selected="false" aria-controls="filesTab">文件</button>
      </div>
      <section id="statusTab" class="panel tab-pane" role="tabpanel">
        <h2>状态</h2>
        <div id="status" class="kv"></div>
      </section>
      <section id="filesTab" class="panel tab-pane" role="tabpanel" hidden>
        <div class="file-toolbar" role="toolbar" aria-label="NAND 文件操作">
          <button id="fileUp" class="secondary" title="上级目录" aria-label="上级目录">←</button>
          <button id="fileRefresh" class="secondary" title="刷新" aria-label="刷新">↻</button>
          <button id="fileMkdir" class="secondary">+ 文件夹</button>
          <button id="fileImport">⇧ 导入</button>
          <input id="fileImportInput" type="file" hidden>
        </div>
        <div id="filePath" class="file-path">A:\</div>
        <div id="fileList" class="file-list"></div>
        <div id="fileManagerStatus" class="file-manager-status"></div>
      </section>
    </aside>
  </div>
  <button id="drawerBackdrop" class="drawer-backdrop" title="关闭抽屉" aria-label="关闭抽屉" hidden></button>
  <dialog id="settingsDialog" class="settings-dialog" aria-labelledby="settingsTitle">
    <div class="settings-dialog-shell">
      <header class="settings-dialog-header">
        <h2 id="settingsTitle">设置</h2>
        <button id="closeSettings" class="settings-close" title="关闭" aria-label="关闭设置">×</button>
      </header>
      <div class="settings-tabs" role="tablist" aria-label="设置分类">
        <button id="keymapSettingsTab" class="settings-tab active" role="tab" aria-selected="true" aria-controls="keymapSettingsPane">按键映射</button>
        <button id="touchSettingsTab" class="settings-tab" role="tab" aria-selected="false" aria-controls="touchSettingsPane">触摸</button>
        <button id="powerSettingsTab" class="settings-tab" role="tab" aria-selected="false" aria-controls="powerSettingsPane">电源</button>
      </div>
      <div class="settings-dialog-body">
        <section id="keymapSettingsPane" class="settings-pane keymap-panel" role="tabpanel" aria-labelledby="keymapSettingsTab">
          <div class="keymap-header">
            <h2>输入映射</h2>
            <button id="resetKeyBindings" class="secondary icon-button" title="恢复默认映射" aria-label="恢复默认映射">↺</button>
          </div>
          <div class="binding-grid">
            <div class="binding-control"><span>上</span><button data-binding-code="4">W / 手柄↑</button></div>
            <div class="binding-control"><span>下</span><button data-binding-code="5">S / 手柄↓</button></div>
            <div class="binding-control"><span>左</span><button data-binding-code="6">A / 手柄←</button></div>
            <div class="binding-control"><span>右</span><button data-binding-code="7">D / 手柄→</button></div>
            <div class="binding-control"><span>确定</span><button data-binding-code="10">J / 手柄 B0</button></div>
            <div class="binding-control"><span>退出</span><button data-binding-code="9">K / 手柄 B1</button></div>
          </div>
          <div id="gamepadStatus" class="gamepad-status" role="status">未检测到手柄；请按任意手柄键</div>
        </section>
        <section id="touchSettingsPane" class="settings-pane" role="tabpanel" aria-labelledby="touchSettingsTab" hidden>
          <label class="check"><input id="frontendInputCalibration" type="checkbox">前端输入校准</label>
        </section>
        <section id="powerSettingsPane" class="settings-pane" role="tabpanel" aria-labelledby="powerSettingsTab" hidden>
          <div class="setting-row"><span>USB 供电</span><label class="check"><input id="usbPowerConnected" type="checkbox" checked>已连接</label></div>
        </section>
      </div>
    </div>
  </dialog>
<script>
const screen = document.getElementById('screen');
const screenCtx = screen.getContext('2d', { alpha: false });
screenCtx.imageSmoothingEnabled = false;
const statusEl = document.getElementById('status');
const frontendInputCalibrationEl = document.getElementById('frontendInputCalibration');
const usbPowerConnectedEl = document.getElementById('usbPowerConnected');
const imageStatusEl = document.getElementById('imageStatus');
const openSettingsEl = document.getElementById('openSettings');
const settingsDialogEl = document.getElementById('settingsDialog');
const closeSettingsEl = document.getElementById('closeSettings');
const keymapSettingsTabEl = document.getElementById('keymapSettingsTab');
const touchSettingsTabEl = document.getElementById('touchSettingsTab');
const powerSettingsTabEl = document.getElementById('powerSettingsTab');
const keymapSettingsPaneEl = document.getElementById('keymapSettingsPane');
const touchSettingsPaneEl = document.getElementById('touchSettingsPane');
const powerSettingsPaneEl = document.getElementById('powerSettingsPane');
const rotateLeftEl = document.getElementById('rotateLeft');
const rotateRightEl = document.getElementById('rotateRight');
const toggleAudioEl = document.getElementById('toggleAudio');
const toggleFullscreenEl = document.getElementById('toggleFullscreen');
const exitFullscreenEl = document.getElementById('exitFullscreen');
const screenWrapEl = document.getElementById('screenWrap');
const screenStopOverlayEl = document.getElementById('screenStopOverlay');
const screenStopTitleEl = document.getElementById('screenStopTitle');
const screenStopMessageEl = document.getElementById('screenStopMessage');
const restartFromOverlayEl = document.getElementById('restartFromOverlay');
const stopEl = document.getElementById('stop');
const resetEl = document.getElementById('reset');
const forceStopEl = document.getElementById('forceStop');
const saveFeedbackEl = document.getElementById('saveFeedback');
const operationStatusEl = document.getElementById('operationStatus');
const powerKeyEl = document.getElementById('powerKey');
const deviceKeyEls = [...document.querySelectorAll('.device-key')];
const orientationLabelEl = document.getElementById('orientationLabel');
const resetKeyBindingsEl = document.getElementById('resetKeyBindings');
const gamepadStatusEl = document.getElementById('gamepadStatus');
const controlsDrawerEl = document.getElementById('controlsDrawer');
const statusDrawerEl = document.getElementById('statusDrawer');
const openControlsDrawerEl = document.getElementById('openControlsDrawer');
const openStatusDrawerEl = document.getElementById('openStatusDrawer');
const drawerBackdropEl = document.getElementById('drawerBackdrop');
const statusTabButtonEl = document.getElementById('statusTabButton');
const filesTabButtonEl = document.getElementById('filesTabButton');
const statusTabEl = document.getElementById('statusTab');
const filesTabEl = document.getElementById('filesTab');
const filePathEl = document.getElementById('filePath');
const fileListEl = document.getElementById('fileList');
const fileManagerStatusEl = document.getElementById('fileManagerStatus');
const fileImportInputEl = document.getElementById('fileImportInput');
let poller = null;
let framePoller = null;
let framePollInFlight = false;
let lifecycleRequestPending = false;
let feedbackRequestPending = false;
let ws = null;
let wsOpenPromise = null;
let wsWatchdog = null;
let wsLastMessageAt = 0;
let audioWs = null;
let audioWsReconnectTimer = null;
let audioContext = null;
let audioGain = null;
let audioNextTime = 0;
let audioEnabled = true;
let audioPacketsReceived = 0;
let audioPacketsScheduled = 0;
let audioPacketsRejected = 0;
let audioLastError = '';
let audioClockValue = 0;
let audioClockObservedAt = 0;
let audioClockStalled = false;
const scheduledAudioSources = new Set();
let pointerActive = false;
let activePointerId = null;
let touchDownAt = 0;
let pendingTouchReleaseTimer = null;
let pendingTouchMove = null;
let pendingTouchMoveFrame = null;
let pendingTouchMoveTimer = null;
let lastTouchMoveSentAt = 0;
let touchMoveAwaitingFrame = false;
let currentOrientation = 'rot180';
let pendingOrientation = null;
let lastRawFrameBuffer = null;
let rgb565Lut = null;
let rawImageData = null;
let bindingCaptureCode = null;
let currentSidebarTab = 'status';
let currentSettingsTab = 'keymap';
let activeDrawer = null;
let currentNandDirectory = '/';
let nandFilesBusy = false;
const mobileLayoutQuery = window.matchMedia('(max-width: 760px)');
const minTouchHoldMs = 180;
const minTouchMoveIntervalMs = 1000 / 30;
const touchMoveBackpressureMs = 1000 / 30;
const minKeyHoldMs = 100;
const wsIdleReconnectMs = 5000;
const audioPacketHeaderBytes = 24;
const audioTargetLeadSeconds = 0.06;
const audioMaximumLeadSeconds = 0.3;
const keyBindingStorageKey = 'bbk9588.keyBindings.v1';
const gamepadBindingStorageKey = 'bbk9588.gamepadBindings.v1';
const inputSessionId = window.crypto?.randomUUID?.() ||
  `web-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
const keyLeaseHeartbeatMs = 250;
const defaultKeyBindings = Object.freeze({
  4:'KeyW',
  5:'KeyS',
  6:'KeyA',
  7:'KeyD',
  9:'KeyK',
  10:'KeyJ',
});
const defaultGamepadBindings = Object.freeze({
  4:'button:12',
  5:'button:13',
  6:'button:14',
  7:'button:15',
  9:'button:1',
  10:'button:0',
});
const legacyDefaultKeyBindings = Object.freeze({9:'Escape', 10:'Space'});
const legacyDefaultGamepadBindings = Object.freeze({
  4:'axis:1:-',
  5:'axis:1:+',
  6:'axis:0:-',
  7:'axis:0:+',
});
const rotationOrientations = ['raw', 'cw90', 'rot180', 'ccw90'];
const orientationLabels = {raw:'0°', cw90:'90°', rot180:'180°', ccw90:'270°', hflip:'水平', vflip:'垂直'};
let keyBindings = loadKeyBindings();
let gamepadBindings = loadGamepadBindings();

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    const body = await res.text();
    try {
      const detail = JSON.parse(body);
      throw new Error(detail.error || body);
    } catch (err) {
      if (err instanceof SyntaxError) throw new Error(body);
      throw err;
    }
  }
  return await res.json();
}
function commandFetchFallback(msg) {
  return fetch('/api/command', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify(msg)
  }).then(r => r.json()).then(renderStatus);
}
function ensurePolling() {
  if (!poller) poller = setInterval(() => refresh().catch(console.error), 1000);
  ensureFramePolling();
}
function ensureFramePolling() {
  if (!framePoller) {
    framePoller = setInterval(() => refreshFrameFallback().catch(console.error), 250);
    refreshFrameFallback().catch(console.error);
  }
}
function wsIsStale() {
  return ws && ws.readyState === WebSocket.OPEN && performance.now() - wsLastMessageAt > wsIdleReconnectMs;
}
function dropWs(reason = 'stale websocket') {
  const sock = ws;
  ws = null;
  wsOpenPromise = null;
  stopWsWatchdog();
  ensurePolling();
  if (!sock || sock.readyState === WebSocket.CLOSED) return;
  try { sock.close(4000, reason); } catch (err) { console.error(err); }
}
function wsSend(msg) {
  if (wsIsStale()) {
    dropWs();
    connectWs().catch(() => {});
    return commandFetchFallback(msg);
  }
  if (ws && ws.readyState === WebSocket.OPEN) {
    try {
      ws.send(JSON.stringify(msg));
      return Promise.resolve();
    } catch (err) {
      dropWs('websocket send failed');
      connectWs().catch(() => {});
      return commandFetchFallback(msg);
    }
  }
  if (!ws || ws.readyState === WebSocket.CLOSING || ws.readyState === WebSocket.CLOSED) {
    connectWs().catch(() => {});
  }
  if (wsOpenPromise) {
    return wsOpenPromise.then(sock => {
      if (!sock || sock.readyState !== WebSocket.OPEN) return commandFetchFallback(msg);
      sock.send(JSON.stringify(msg));
      return undefined;
    }).catch(() => commandFetchFallback(msg));
  }
  return commandFetchFallback(msg);
}
function screenPointFromClient(clientX, clientY, clamp = false) {
  const r = screen.getBoundingClientRect();
  if (!clamp && (clientX < r.left || clientX >= r.right || clientY < r.top || clientY >= r.bottom)) return null;
  const displayWidth = screen.width || 240;
  const displayHeight = screen.height || 320;
  let x = Math.floor((clientX - r.left) * displayWidth / r.width);
  let y = Math.floor((clientY - r.top) * displayHeight / r.height);
  if (!clamp && (x < 0 || x >= displayWidth || y < 0 || y >= displayHeight)) return null;
  x = Math.max(0, Math.min(displayWidth - 1, x));
  y = Math.max(0, Math.min(displayHeight - 1, y));
  return {x, y, width: displayWidth, height: displayHeight};
}
function sendTouchAt(clientX, clientY, down, phase, source = 'pointer', clamp = false) {
  const p = screenPointFromClient(clientX, clientY, clamp);
  if (!p) return false;
  wsSend({
    op:'touch',
    display_x:p.x,
    display_y:p.y,
    display_width:p.width,
    display_height:p.height,
    down,
    phase,
    source,
    advance:false,
    reply:false
  });
  return true;
}
function cancelPendingTouchRelease() {
  if (pendingTouchReleaseTimer) {
    clearTimeout(pendingTouchReleaseTimer);
    pendingTouchReleaseTimer = null;
  }
}
function clearPendingTouchMove() {
  pendingTouchMove = null;
  if (pendingTouchMoveFrame !== null) {
    cancelAnimationFrame(pendingTouchMoveFrame);
    pendingTouchMoveFrame = null;
  }
  if (pendingTouchMoveTimer !== null) {
    clearTimeout(pendingTouchMoveTimer);
    pendingTouchMoveTimer = null;
  }
}
function flushPendingTouchMove() {
  if (!pendingTouchMove) {
    clearPendingTouchMove();
    return false;
  }
  const move = pendingTouchMove;
  clearPendingTouchMove();
  const sent = sendTouchAt(move.clientX, move.clientY, true, 'move', move.source, true);
  if (sent) {
    lastTouchMoveSentAt = performance.now();
    touchMoveAwaitingFrame = true;
  }
  return sent;
}
function schedulePendingTouchMove() {
  if (pendingTouchMoveFrame !== null || pendingTouchMoveTimer !== null) return;
  const elapsed = performance.now() - lastTouchMoveSentAt;
  const rateDelay = minTouchMoveIntervalMs - elapsed;
  const frameDelay = touchMoveAwaitingFrame ? touchMoveBackpressureMs - elapsed : 0;
  const delay = Math.max(0, rateDelay, frameDelay);
  if (delay > 0) {
    pendingTouchMoveTimer = setTimeout(() => {
      pendingTouchMoveTimer = null;
      schedulePendingTouchMove();
    }, delay);
    return;
  }
  pendingTouchMoveFrame = requestAnimationFrame(() => {
    pendingTouchMoveFrame = null;
    const move = pendingTouchMove;
    pendingTouchMove = null;
    if (move && pointerActive) {
      if (sendTouchAt(move.clientX, move.clientY, true, 'move', move.source, true)) {
        lastTouchMoveSentAt = performance.now();
        touchMoveAwaitingFrame = true;
      }
    }
    if (pendingTouchMove) schedulePendingTouchMove();
  });
}
function queueTouchMove(clientX, clientY, source = 'pointer') {
  pendingTouchMove = {clientX, clientY, source};
  schedulePendingTouchMove();
}
function sendTouchReleaseAt(clientX, clientY, phase, source = 'pointer', clamp = true) {
  flushPendingTouchMove();
  const elapsed = performance.now() - touchDownAt;
  const delay = Math.max(0, minTouchHoldMs - elapsed);
  cancelPendingTouchRelease();
  pendingTouchReleaseTimer = setTimeout(() => {
    pendingTouchReleaseTimer = null;
    sendTouchAt(clientX, clientY, false, phase, source, clamp);
    touchMoveAwaitingFrame = false;
  }, delay);
}
function noteScreenFrame() {
  if (!touchMoveAwaitingFrame) return;
  touchMoveAwaitingFrame = false;
  if (pendingTouchMove && pointerActive) schedulePendingTouchMove();
}
function formatElapsed(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(Number(seconds))) return '';
  const total = Math.max(0, Math.floor(Number(seconds)));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h) return `${h}h ${String(m).padStart(2, '0')}m ${String(s).padStart(2, '0')}s`;
  if (m) return `${m}m ${String(s).padStart(2, '0')}s`;
  return `${s}s`;
}
function basename(path) {
  return String(path || '').split(/[\\/]/).pop() || String(path || '');
}
function firstNumber(...values) {
  for (const value of values) {
    if (value !== null && value !== undefined && !Number.isNaN(Number(value))) return Number(value);
  }
  return null;
}
function formatRate(value, unit, fallback = 'n/a') {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return fallback;
  return `${Number(value).toFixed(1)} ${unit}`;
}
function formatPercent(value, fallback = 'n/a') {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return fallback;
  return `${Number(value).toFixed(1)}%`;
}
function formatGuestIps(perf) {
  if (!perf || !perf.guest_ips_available) return 'n/a';
  const ips = Number(perf.guest_ips || 0);
  if (ips >= 1000000) return `${(ips / 1000000).toFixed(1)} Mips`;
  if (ips >= 1000) return `${(ips / 1000).toFixed(1)} Kips`;
  return `${ips.toFixed(0)} ips`;
}
function formatCounter(value) {
  const count = Number(value || 0);
  if (count >= 1000000) return `${(count / 1000000).toFixed(1)}M`;
  if (count >= 1000) return `${(count / 1000).toFixed(1)}K`;
  return `${Math.max(0, Math.floor(count))}`;
}
function formatAudioMode(audio) {
  if (!audio || !audio.packet_count) return 'waiting';
  const mode = audio.playing && audio.recording ? 'play+rec' :
    audio.playing ? 'play' : audio.recording ? 'record' : 'idle';
  const rate = audio.sample_rate_hz ? ` ${audio.sample_rate_hz} Hz` : '';
  return `${mode}${rate}${audio.muted ? ' muted' : ''}`;
}
function formatWebAudio(transport) {
  const state = !audioEnabled ? 'muted' : audioContext?.state || 'locked';
  const packets = `${audioPacketsScheduled}/${audioPacketsReceived}`;
  const error = audioLastError ? ` ${audioLastError}` : '';
  return `${state} ${packets} / ${transport?.ws_connections ?? 0}${error}`;
}
function syncDrawerState() {
  const mobile = mobileLayoutQuery.matches;
  const controlsOpen = mobile && activeDrawer === 'controls';
  const statusOpen = mobile && activeDrawer === 'status';
  controlsDrawerEl.classList.toggle('drawer-open', controlsOpen);
  statusDrawerEl.classList.toggle('drawer-open', statusOpen);
  openControlsDrawerEl.setAttribute('aria-expanded', String(controlsOpen));
  openStatusDrawerEl.setAttribute('aria-expanded', String(statusOpen));
  drawerBackdropEl.hidden = !(controlsOpen || statusOpen);
  controlsDrawerEl.inert = mobile && !controlsOpen;
  statusDrawerEl.inert = mobile && !statusOpen;
  if (mobile && !controlsOpen) controlsDrawerEl.setAttribute('aria-hidden', 'true');
  else controlsDrawerEl.removeAttribute('aria-hidden');
  if (mobile && !statusOpen) statusDrawerEl.setAttribute('aria-hidden', 'true');
  else statusDrawerEl.removeAttribute('aria-hidden');
}
function openDrawer(name) {
  if (!mobileLayoutQuery.matches) return;
  activeDrawer = name === 'status' ? 'status' : 'controls';
  syncDrawerState();
}
function closeDrawers() {
  activeDrawer = null;
  syncDrawerState();
}
function setSidebarTab(tab) {
  currentSidebarTab = tab === 'files' ? 'files' : 'status';
  const filesActive = currentSidebarTab === 'files';
  statusTabButtonEl.classList.toggle('active', !filesActive);
  filesTabButtonEl.classList.toggle('active', filesActive);
  statusTabButtonEl.setAttribute('aria-selected', String(!filesActive));
  filesTabButtonEl.setAttribute('aria-selected', String(filesActive));
  statusTabEl.hidden = filesActive;
  filesTabEl.hidden = !filesActive;
  if (filesActive) loadNandFiles(currentNandDirectory).catch(showFileManagerError);
}
function setSettingsTab(tab) {
  currentSettingsTab = ['keymap', 'touch', 'power'].includes(tab) ? tab : 'keymap';
  const touchActive = currentSettingsTab === 'touch';
  const keymapActive = currentSettingsTab === 'keymap';
  const powerActive = currentSettingsTab === 'power';
  keymapSettingsTabEl.classList.toggle('active', keymapActive);
  touchSettingsTabEl.classList.toggle('active', touchActive);
  powerSettingsTabEl.classList.toggle('active', powerActive);
  keymapSettingsTabEl.setAttribute('aria-selected', String(keymapActive));
  touchSettingsTabEl.setAttribute('aria-selected', String(touchActive));
  powerSettingsTabEl.setAttribute('aria-selected', String(powerActive));
  keymapSettingsPaneEl.hidden = !keymapActive;
  touchSettingsPaneEl.hidden = !touchActive;
  powerSettingsPaneEl.hidden = !powerActive;
  if (!keymapActive && bindingCaptureCode !== null) {
    bindingCaptureCode = null;
    updateKeyBindingUi();
  }
}
function openSettingsDialog() {
  closeDrawers();
  releaseButtonInputs('settings-open');
  releaseKeyboardInputs('settings-open');
  releaseGamepadInputs('settings-open');
  setSettingsTab(currentSettingsTab);
  if (!settingsDialogEl.open) settingsDialogEl.showModal();
}
function closeSettingsDialog() {
  bindingCaptureCode = null;
  updateKeyBindingUi();
  if (settingsDialogEl.open) settingsDialogEl.close();
}
function formatFileSize(value) {
  const size = Math.max(0, Number(value || 0));
  if (size >= 1024 * 1024) return `${(size / (1024 * 1024)).toFixed(1)} MB`;
  if (size >= 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${size} B`;
}
function displayNandPath(path) {
  const normalized = String(path || '/').replaceAll('/', '\\');
  return normalized === '\\' ? 'A:\\' : `A:${normalized}`;
}
function showFileManagerError(error) {
  fileManagerStatusEl.textContent = String(error?.message || error || '文件操作失败');
}
function setNandFilesBusy(busy, message = '') {
  nandFilesBusy = Boolean(busy);
  for (const button of filesTabEl.querySelectorAll('button')) button.disabled = nandFilesBusy;
  fileImportInputEl.disabled = nandFilesBusy;
  fileManagerStatusEl.textContent = message;
}
function renderNandFiles(result) {
  currentNandDirectory = String(result.path || '/');
  filePathEl.textContent = displayNandPath(currentNandDirectory);
  document.getElementById('fileUp').disabled = nandFilesBusy || currentNandDirectory === '/';
  const entries = Array.isArray(result.entries) ? result.entries : [];
  const rows = [];
  for (const entry of entries) {
    const row = document.createElement('div');
    row.className = 'file-row';
    const nameButton = document.createElement('button');
    nameButton.className = 'file-name';
    nameButton.title = String(entry.name || '');
    const icon = document.createElement('span');
    icon.textContent = entry.is_dir ? '▸' : '·';
    const nameText = document.createElement('span');
    nameText.className = 'file-name-text';
    nameText.textContent = String(entry.name || '');
    const sizeText = document.createElement('span');
    sizeText.className = 'file-size';
    sizeText.textContent = entry.is_dir ? '' : formatFileSize(entry.size);
    nameButton.append(icon, nameText, sizeText);
    if (entry.is_dir) {
      nameButton.onclick = () => loadNandFiles(entry.path).catch(showFileManagerError);
    } else {
      nameButton.onclick = () => exportNandFile(entry.path);
    }
    const actions = document.createElement('div');
    actions.className = 'file-actions';
    if (!entry.is_dir) {
      const exportButton = document.createElement('button');
      exportButton.textContent = '↓';
      exportButton.title = '导出';
      exportButton.setAttribute('aria-label', `导出 ${entry.name}`);
      exportButton.onclick = () => exportNandFile(entry.path);
      actions.appendChild(exportButton);
    }
    const renameButton = document.createElement('button');
    renameButton.textContent = '✎';
    renameButton.title = '改名';
    renameButton.setAttribute('aria-label', `改名 ${entry.name}`);
    renameButton.onclick = () => renameNandEntry(entry);
    const deleteButton = document.createElement('button');
    deleteButton.className = 'delete';
    deleteButton.textContent = '×';
    deleteButton.title = '删除';
    deleteButton.setAttribute('aria-label', `删除 ${entry.name}`);
    deleteButton.onclick = () => deleteNandEntry(entry);
    actions.append(renameButton, deleteButton);
    row.append(nameButton, actions);
    rows.push(row);
  }
  if (!rows.length) {
    const empty = document.createElement('div');
    empty.className = 'file-empty';
    empty.textContent = '空文件夹';
    rows.push(empty);
  }
  fileListEl.replaceChildren(...rows);
  fileManagerStatusEl.textContent = `${entries.length} 项`;
}
async function loadNandFiles(path = '/') {
  if (nandFilesBusy) return;
  setNandFilesBusy(true, '正在读取 NAND...');
  try {
    renderNandFiles(await api(`/api/files?path=${encodeURIComponent(path)}`));
  } finally {
    setNandFilesBusy(false, fileManagerStatusEl.textContent);
    document.getElementById('fileUp').disabled = currentNandDirectory === '/';
  }
}
async function runNandFileMutation(url, options, message) {
  if (nandFilesBusy) return;
  setNandFilesBusy(true, `${message}，模拟器将重启...`);
  try {
    const status = await api(url, options);
    renderStatus(status);
    await loadNandFilesAfterMutation();
    connectWs().catch(console.error);
  } catch (error) {
    showFileManagerError(error);
  } finally {
    setNandFilesBusy(false, fileManagerStatusEl.textContent);
  }
}
async function loadNandFilesAfterMutation() {
  const result = await api(`/api/files?path=${encodeURIComponent(currentNandDirectory)}`);
  renderNandFiles(result);
}
function jsonRequest(body) {
  return {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)};
}
function exportNandFile(path) {
  window.location.assign(`/api/files/export?path=${encodeURIComponent(path)}`);
}
function renameNandEntry(entry) {
  const name = window.prompt('新名称', String(entry.name || ''));
  if (!name || name === entry.name) return;
  runNandFileMutation('/api/files/rename', jsonRequest({path:entry.path, name}), '正在改名');
}
function deleteNandEntry(entry) {
  const kind = entry.is_dir ? '文件夹及其中全部内容' : '文件';
  if (!window.confirm(`确定删除${kind}“${entry.name}”？`)) return;
  runNandFileMutation('/api/files/delete', jsonRequest({path:entry.path}), '正在删除');
}
function parentNandDirectory(path) {
  const parts = String(path || '/').split('/').filter(Boolean);
  parts.pop();
  return parts.length ? `/${parts.join('/')}` : '/';
}
function qemuCp0Status(s) {
  return s.cp0 || s.qemu?.cp0 || null;
}
function formatQemuException(s) {
  const cp0 = qemuCp0Status(s);
  if (!cp0) return '';
  const exc = cp0.exception || '';
  if (!exc) return '';
  if (exc === 'interrupt' && !cp0.exl && !cp0.erl && cp0.pending_enabled_interrupts === '0x00') {
    return '';
  }
  return exc;
}
function formatQemuIrq(s) {
  const cp0 = qemuCp0Status(s);
  if (!cp0) return '';
  const pending = cp0.pending_interrupts || '';
  const enabled = cp0.pending_enabled_interrupts || '';
  const suffix = cp0.exception === 'interrupt' && !cp0.exl && !cp0.erl && enabled === '0x00' ? ' pending' : '';
  return `${pending}/${enabled}${suffix}`;
}
function formatLastInput(s) {
  const ev = s.last_input_event;
  if (!ev) return '';
  const age = ev.at ? `${Math.max(0, (Date.now() / 1000 - Number(ev.at))).toFixed(1)}s` : '';
  const result = ev.result || {};
  if (ev.kind === 'touch') {
    const display = ev.display_x !== undefined ? ` d=${ev.display_x},${ev.display_y}` : '';
    return `${ev.down ? 'down' : 'up'} ${ev.x},${ev.y}${display} ${ev.accepted ? 'ok' : 'fail'} writes=${result.bbk_input_write_count ?? ''} ${age}`;
  }
  if (ev.kind === 'key') {
    return `${ev.down ? 'down' : 'up'} ${ev.code} ${ev.accepted ? 'ok' : 'fail'} writes=${result.bbk_input_write_count ?? ''} ${age}`;
  }
  return JSON.stringify(ev);
}
function currentFullscreenElement() {
  return document.fullscreenElement || document.webkitFullscreenElement || null;
}
function updateFullscreenScreenSize() {
  const active = currentFullscreenElement() === screenWrapEl;
  toggleFullscreenEl.title = active ? '退出全屏' : '全屏';
  toggleFullscreenEl.setAttribute('aria-label', active ? '退出全屏' : '全屏');
  const wrapStyle = getComputedStyle(screenWrapEl);
  const horizontalPadding = parseFloat(wrapStyle.paddingLeft) + parseFloat(wrapStyle.paddingRight);
  const verticalPadding = parseFloat(wrapStyle.paddingTop) + parseFloat(wrapStyle.paddingBottom);
  const availableWidth = Math.max(1, screenWrapEl.clientWidth - horizontalPadding);
  const availableHeight = Math.max(1, screenWrapEl.clientHeight - verticalPadding);
  const scale = Math.min(availableWidth / screen.width, availableHeight / screen.height);
  screen.style.width = `${Math.max(1, Math.floor(screen.width * scale))}px`;
  screen.style.height = `${Math.max(1, Math.floor(screen.height * scale))}px`;
}
async function toggleFullscreen() {
  try {
    if (currentFullscreenElement()) {
      const exit = document.exitFullscreen || document.webkitExitFullscreen;
      if (exit) await exit.call(document);
      return;
    }
    const request = screenWrapEl.requestFullscreen || screenWrapEl.webkitRequestFullscreen;
    if (!request) throw new Error('当前浏览器不支持全屏 API');
    await request.call(screenWrapEl);
  } catch (err) {
    console.error(err);
  }
}
function updateOrientationControls() {
  orientationLabelEl.textContent = orientationLabels[currentOrientation] || currentOrientation;
  const disabled = pendingOrientation !== null;
  rotateLeftEl.disabled = disabled;
  rotateRightEl.disabled = disabled;
}
function applyFrontendOrientation(orientation) {
  if (!orientation || !(orientation in orientationLabels)) return;
  const changed = currentOrientation !== orientation;
  currentOrientation = orientation;
  if (pendingOrientation === orientation) pendingOrientation = null;
  updateOrientationControls();
  if (changed && lastRawFrameBuffer) {
    requestAnimationFrame(() => drawRawRgb565Frame(lastRawFrameBuffer));
  }
}
function requestRotation(delta) {
  if (pendingOrientation !== null) return;
  let index = rotationOrientations.indexOf(currentOrientation);
  if (index < 0) index = rotationOrientations.indexOf('rot180');
  const next = rotationOrientations[(index + delta + rotationOrientations.length) % rotationOrientations.length];
  pendingOrientation = next;
  updateOrientationControls();
  wsSend({op:'set-orientation', orientation:next}).catch(err => {
    console.error(err);
    pendingOrientation = null;
    updateOrientationControls();
  });
  setTimeout(() => {
    if (pendingOrientation !== next) return;
    refresh().catch(console.error).finally(() => {
      if (pendingOrientation === next) {
        pendingOrientation = null;
        updateOrientationControls();
      }
    });
  }, 1200);
}
function renderStatus(s) {
  applyFrontendOrientation(s.orientation || currentOrientation);
  frontendInputCalibrationEl.checked = Boolean(s.frontend_input_calibration);
  usbPowerConnectedEl.checked = Boolean(s.usb_power_connected);
  imageStatusEl.textContent = basename(s.nand_image) || 'bbk9588_nand.bin';
  const qemuPerf = s.qemu?.performance || {};
  const qemuAudio = qemuPerf.audio || {};
  const audioTransport = s.audio_transport || {};
  const frontendPerf = s.frontend_performance || {};
  renderLifecycle(s);
  const rows = [
    ['running', s.running],
    ['since reset', formatElapsed(s.reset_elapsed_seconds ?? s.emulator_elapsed_seconds)],
    ['qemu fps', formatRate(firstNumber(qemuPerf.frame_chardev_fps, qemuPerf.frame_chardev_average_fps), 'fps')],
    ['web fps', formatRate(frontendPerf.websocket_fps, 'fps')],
    ['web tx', formatRate(frontendPerf.websocket_transport_fps, 'fps')],
    ['ws clients', s.frame_push?.ws_connections ?? 0],
    ['png fps', formatRate(frontendPerf.screen_png_fps, 'fps')],
    ['qemu cpu', formatPercent(firstNumber(qemuPerf.qemu_cpu_one_core_percent, qemuPerf.qemu_cpu_host_percent))],
    ['guest ips', formatGuestIps(qemuPerf)],
    ['audio', formatAudioMode(qemuAudio)],
    ['web audio', formatWebAudio(audioTransport)],
    ['audio fifo', `tx ${qemuAudio.tx_fifo_level ?? 0} / rx ${qemuAudio.rx_fifo_level ?? 0}`],
    ['audio dma', `tx ${formatCounter(qemuAudio.tx_dma_samples)} / rx ${formatCounter(qemuAudio.rx_dma_samples)}`],
    ['audio frames', `out ${formatCounter(qemuAudio.output_frames)} / in ${formatCounter(qemuAudio.input_frames)}`],
    ['audio xrun', `${formatCounter(qemuAudio.underruns)} / ${formatCounter(qemuAudio.overruns)}`],
    ['boot', s.boot_mode || ''],
    ['nand', basename(s.nand_image || '')],
    ['nand writes', s.qemu?.nand_write_mode || s.nand_write_mode || 'none'],
    ['usb power', (s.qemu?.usb_power_connected ?? s.usb_power_connected) ? 'connected' : 'disconnected'],
    ['orientation', s.orientation || ''],
    ['input calib', `${s.frontend_input_calibration ? 'on' : 'off'}:${s.frontend_input_calibration_stage_label || s.frontend_input_calibration_stage || 0}`],
    ['touch queue', s.pending_touches ?? 0],
    ['key queue', s.pending_keys ?? 0],
    ['input wake', s.input_wake_count ?? 0],
    ['last input', formatLastInput(s)],
    ['frame queued', `${s.frame_push?.queued_count ?? 0}/${s.queued_frames ?? 0}`],
    ['frame sent', s.frame_push?.ws_sent_count ?? 0],
    ['push lag', `${s.frame_push?.source_lag_ms ?? ''} ms`],
    ['frame skipped', s.frame_push?.replace_count ?? 0],
    ['shutdown', s.safe_shutdown?.state || 'idle'],
    ['shutdown error', s.safe_shutdown?.error || ''],
    ['stop', s.stop_reason || ''],
    ['pc', s.pc],
    ['qemu region', s.qemu_pc_region || s.qemu_pc_classification?.region || s.qemu_pc_classification?.name || ''],
    ['qemu exc', formatQemuException(s)],
    ['qemu irq', formatQemuIrq(s)],
    ['wait', s.scheduler?.wait_wake_count ?? ''],
    ['tick', s.scheduler?.timer_tick_count ?? ''],
    ['dispatch', s.scheduler?.scheduler_dispatch_count ?? ''],
    ['enabled', s.scheduler?.fields?.run_enabled_3f09 ?? ''],
    ['countdown', s.scheduler?.fields?.timer_countdown_3f08 ?? ''],
    ['pixels', s.framebuffer?.nonzero_pixels ?? ''],
    ['bbox', JSON.stringify(s.framebuffer?.nonzero_bbox ?? null)]
  ];
  const statusNodes = [];
  for (const [key, value] of rows) {
    const labelEl = document.createElement('div');
    const valueEl = document.createElement('div');
    const valueText = String(value);
    labelEl.textContent = key;
    valueEl.className = 'kv-value';
    valueEl.textContent = valueText;
    valueEl.title = valueText;
    statusNodes.push(labelEl, valueEl);
  }
  statusEl.replaceChildren(...statusNodes);
}
function lifecyclePresentation(s) {
  const detail = s.stop_detail;
  if (s.running || !detail?.code) return null;
  if (detail.code === 'guest-shutdown') {
    return {title:'设备已关机', message:'系统固件已通过 RTC 电源管理完成关机。', action:'重新启动', error:false};
  }
  if (detail.code === 'user-stop') {
    return {title:'模拟器已停止', message:'模拟器已由用户停止。', action:'启动', error:false};
  }
  if (detail.code === 'guest-reset') {
    return {title:'设备请求重启', message:'系统固件请求了重启；QEMU 按当前的不自动重启设置退出。', action:'重新启动', error:false};
  }
  if (detail.code === 'process-exit') {
    return {title:'模拟器已退出', message:'QEMU 已正常退出，但未收到明确的设备关机原因。', action:'重新启动', error:false};
  }
  const suffix = detail.returncode == null ? '' : `（退出码 ${detail.returncode}）`;
  const error = detail.error ? ` ${detail.error}` : '';
  return {title:'模拟器异常退出', message:`QEMU 未正常运行${suffix}。${error}`, action:'重新启动', error:true};
}
function renderLifecycle(s) {
  const presentation = lifecyclePresentation(s);
  screenStopOverlayEl.hidden = !presentation;
  stopEl.disabled = !s.running || lifecycleRequestPending;
  resetEl.disabled = lifecycleRequestPending;
  forceStopEl.disabled = !s.running || lifecycleRequestPending;
  powerKeyEl.disabled = !s.running || lifecycleRequestPending;
  usbPowerConnectedEl.disabled = !s.running || lifecycleRequestPending;
  for (const button of deviceKeyEls) button.disabled = !s.running || lifecycleRequestPending;
  if (!presentation) return;
  screenStopOverlayEl.classList.toggle('error', presentation.error);
  screenStopTitleEl.textContent = presentation.title;
  screenStopMessageEl.textContent = presentation.message;
  restartFromOverlayEl.textContent = presentation.action;
}
async function refresh() { renderStatus(await api('/api/status')); }
async function refreshFrameFallback() {
  if (framePollInFlight || (ws && ws.readyState === WebSocket.OPEN)) return;
  framePollInFlight = true;
  try {
    const res = await fetch(`/screen.png?fallback=${Date.now()}`, {cache:'no-store'});
    if (!res.ok) throw new Error(await res.text());
    await drawPngFrame(await res.blob());
  } finally {
    framePollInFlight = false;
  }
}
function ensureScreenSize(width, height) {
  screen.classList.toggle('landscape', width > height);
  if (screen.width !== width || screen.height !== height) {
    screen.width = width;
    screen.height = height;
    screenCtx.imageSmoothingEnabled = false;
    rawImageData = null;
  }
  updateFullscreenScreenSize();
}
function reusableImageData(width, height) {
  if (!rawImageData || rawImageData.width !== width || rawImageData.height !== height) {
    rawImageData = screenCtx.createImageData(width, height);
  }
  return rawImageData;
}
function outputSizeForRaw(width, height, orientation) {
  if (orientation === 'cw90' || orientation === 'ccw90') return [height, width];
  return [width, height];
}
function ensureRgb565Lut() {
  if (rgb565Lut) return rgb565Lut;
  const r = new Uint8Array(65536);
  const g = new Uint8Array(65536);
  const b = new Uint8Array(65536);
  for (let px = 0; px < 65536; px++) {
    r[px] = Math.round(((px >> 11) & 0x1f) * 255 / 31);
    g[px] = Math.round(((px >> 5) & 0x3f) * 255 / 63);
    b[px] = Math.round((px & 0x1f) * 255 / 31);
  }
  rgb565Lut = {r, g, b};
  return rgb565Lut;
}
function drawRawRgb565Frame(buffer) {
  if (!(buffer instanceof ArrayBuffer) || buffer.byteLength < 20) return false;
  const bytes = new Uint8Array(buffer);
  const magic = [0x42, 0x42, 0x4b, 0x52, 0x41, 0x57, 0x31, 0x00];
  for (let i = 0; i < magic.length; i++) {
    if (bytes[i] !== magic[i]) return false;
  }
  const view = new DataView(buffer);
  const width = view.getUint16(12, true);
  const height = view.getUint16(14, true);
  const stride = view.getUint16(16, true);
  const format = view.getUint16(18, true);
  if (format !== 1 || width <= 0 || height <= 0 || stride < width) return false;
  lastRawFrameBuffer = buffer;
  const raw = new Uint8Array(buffer, 20);
  if (raw.length < stride * height * 2) return false;
  const [outW, outH] = outputSizeForRaw(width, height, currentOrientation);
  ensureScreenSize(outW, outH);
  const image = reusableImageData(outW, outH);
  const out = image.data;
  const lut = ensureRgb565Lut();
  let outIndex = 0;
  if (currentOrientation === 'rot180') {
    for (let y = 0; y < height; y++) {
      let i = ((height - 1 - y) * stride + (width - 1)) * 2;
      for (let x = 0; x < width; x++, i -= 2) {
        const px = raw[i] | (raw[i + 1] << 8);
        out[outIndex++] = lut.r[px];
        out[outIndex++] = lut.g[px];
        out[outIndex++] = lut.b[px];
        out[outIndex++] = 255;
      }
    }
  } else if (!currentOrientation || currentOrientation === 'none') {
    for (let y = 0; y < height; y++) {
      let i = y * stride * 2;
      for (let x = 0; x < width; x++, i += 2) {
        const px = raw[i] | (raw[i + 1] << 8);
        out[outIndex++] = lut.r[px];
        out[outIndex++] = lut.g[px];
        out[outIndex++] = lut.b[px];
        out[outIndex++] = 255;
      }
    }
  } else {
    for (let y = 0; y < outH; y++) {
      for (let x = 0; x < outW; x++) {
        let sx = x;
        let sy = y;
        if (currentOrientation === 'hflip') {
          sx = width - 1 - x;
        } else if (currentOrientation === 'vflip') {
          sy = height - 1 - y;
        } else if (currentOrientation === 'cw90') {
          sx = y;
          sy = height - 1 - x;
        } else if (currentOrientation === 'ccw90') {
          sx = width - 1 - y;
          sy = x;
        }
        const i = (sy * stride + sx) * 2;
        const px = raw[i] | (raw[i + 1] << 8);
        out[outIndex++] = lut.r[px];
        out[outIndex++] = lut.g[px];
        out[outIndex++] = lut.b[px];
        out[outIndex++] = 255;
      }
    }
  }
  screenCtx.putImageData(image, 0, 0);
  noteScreenFrame();
  return true;
}
async function drawPngFrame(data) {
  const blob = data instanceof Blob ? data : new Blob([data], {type:'image/png'});
  const bitmap = await createImageBitmap(blob);
  ensureScreenSize(bitmap.width, bitmap.height);
  screenCtx.drawImage(bitmap, 0, 0);
  bitmap.close?.();
  noteScreenFrame();
}
function stopWsWatchdog() {
  if (wsWatchdog) {
    clearInterval(wsWatchdog);
    wsWatchdog = null;
  }
}
function startWsWatchdog() {
  stopWsWatchdog();
  wsLastMessageAt = performance.now();
  wsWatchdog = setInterval(() => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    if (performance.now() - wsLastMessageAt <= wsIdleReconnectMs) return;
    ws.close(4000, 'stale websocket');
  }, 1000);
}
function connectWs() {
  if (ws && ws.readyState === WebSocket.OPEN) return Promise.resolve(ws);
  if (ws && ws.readyState === WebSocket.CONNECTING) return wsOpenPromise || Promise.resolve(ws);
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${protocol}//${location.host}/ws`);
  wsOpenPromise = new Promise((resolve, reject) => {
    ws.addEventListener('open', () => resolve(ws), {once:true});
    ws.addEventListener('error', () => reject(new Error('websocket failed')), {once:true});
  });
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => {
    stopPolling();
    stopFramePolling();
    startWsWatchdog();
  };
  ws.onmessage = async ev => {
    wsLastMessageAt = performance.now();
    if (ev.data instanceof ArrayBuffer) {
      if (!drawRawRgb565Frame(ev.data)) await drawPngFrame(ev.data);
      return;
    }
    if (ev.data instanceof Blob) {
      await drawPngFrame(ev.data);
      return;
    }
    try { renderStatus(JSON.parse(ev.data)); } catch (err) { console.error(err); }
  };
  ws.onclose = () => {
    stopWsWatchdog();
    ws = null;
    wsOpenPromise = null;
    ensurePolling();
    setTimeout(connectWs, 1500);
  };
  ws.onerror = () => ws?.close();
  return wsOpenPromise;
}

function updateAudioButton() {
  const running = audioContext?.state === 'running' && !audioClockStalled;
  toggleAudioEl.classList.toggle('is-muted', !audioEnabled);
  toggleAudioEl.title = !audioEnabled ? '开启声音' : running ? '关闭声音' : '启动声音';
  toggleAudioEl.setAttribute('aria-label', toggleAudioEl.title);
  toggleAudioEl.classList.toggle('warn', audioEnabled && !running);
  if (audioGain && audioContext) {
    audioGain.gain.setValueAtTime(audioEnabled ? 1 : 0, audioContext.currentTime);
  }
}
function ensureAudioContext() {
  if (audioContext) return audioContext;
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  if (!AudioContextClass) {
    toggleAudioEl.disabled = true;
    toggleAudioEl.title = '浏览器不支持音频';
    return null;
  }
  try {
    audioContext = new AudioContextClass({latencyHint:'interactive'});
  } catch (_) {
    audioContext = new AudioContextClass();
  }
  audioGain = audioContext.createGain();
  audioGain.gain.value = audioEnabled ? 1 : 0;
  audioGain.connect(audioContext.destination);
  audioClockValue = audioContext.currentTime;
  audioClockObservedAt = performance.now();
  audioClockStalled = false;
  audioContext.onstatechange = updateAudioButton;
  return audioContext;
}
function stopScheduledAudio() {
  for (const source of scheduledAudioSources) {
    try { source.stop(); } catch (_) {}
  }
  scheduledAudioSources.clear();
  audioNextTime = 0;
}
function discardAudioContext() {
  stopScheduledAudio();
  const previous = audioContext;
  audioContext = null;
  audioGain = null;
  audioClockStalled = false;
  try { previous?.close(); } catch (_) {}
}
function primeAudioContext(context) {
  const source = context.createBufferSource();
  source.buffer = context.createBuffer(1, 1, context.sampleRate || 44100);
  source.connect(audioGain || context.destination);
  source.start(0);
}
function observeAudioClock(context) {
  if (context.state !== 'running') return false;
  const now = performance.now();
  const current = context.currentTime;
  if (current > audioClockValue + 0.005) {
    audioClockValue = current;
    audioClockObservedAt = now;
    audioClockStalled = false;
  } else if (now - audioClockObservedAt > 1200) {
    audioClockStalled = true;
  }
  return audioClockStalled;
}
function unlockAudio() {
  if (!audioEnabled) return;
  if (audioContext?.state === 'interrupted' || audioClockStalled) discardAudioContext();
  const context = ensureAudioContext();
  if (!context) return;
  try { primeAudioContext(context); } catch (err) { audioLastError = err?.name || 'prime'; }
  if (context.state !== 'running' && context.state !== 'closed') {
    context.resume().then(() => {
      audioNextTime = 0;
      audioClockValue = context.currentTime;
      audioClockObservedAt = performance.now();
      audioClockStalled = false;
      audioLastError = context.state === 'running' ? '' : context.state;
      updateAudioButton();
    }).catch(err => {
      audioLastError = err?.name || 'resume';
      updateAudioButton();
    });
  } else if (context.state === 'running') {
    audioLastError = '';
    updateAudioButton();
  }
}
function parseAudioPacket(buffer) {
  if (!(buffer instanceof ArrayBuffer) || buffer.byteLength < audioPacketHeaderBytes) return null;
  const magic = new Uint8Array(buffer, 0, 8);
  const expected = [66, 66, 75, 65, 85, 68, 49, 0];
  if (!expected.every((value, index) => magic[index] === value)) return null;
  const view = new DataView(buffer);
  const sequence = view.getUint32(8, true);
  const sampleRate = view.getUint32(12, true);
  const channels = view.getUint16(16, true);
  const bits = view.getUint16(18, true);
  const payloadBytes = view.getUint32(20, true);
  if (bits !== 16 || channels < 1 || channels > 2 ||
      sampleRate < 8000 || sampleRate > 192000 ||
      payloadBytes === 0 || audioPacketHeaderBytes + payloadBytes !== buffer.byteLength ||
      payloadBytes % (channels * 2) !== 0) return null;
  return {sequence, sampleRate, channels, payloadBytes};
}
function scheduleAudioPacket(buffer) {
  if (!audioEnabled) return;
  const context = ensureAudioContext();
  if (!context || context.state !== 'running' || !audioGain) return;
  const packet = parseAudioPacket(buffer);
  if (!packet) {
    audioPacketsRejected += 1;
    return;
  }
  if (observeAudioClock(context)) {
    audioLastError = 'clock stalled';
    updateAudioButton();
    return;
  }
  const frames = packet.payloadBytes / (packet.channels * 2);
  const audioBuffer = context.createBuffer(packet.channels, frames, packet.sampleRate);
  const pcm = new DataView(buffer, audioPacketHeaderBytes, packet.payloadBytes);
  for (let channel = 0; channel < packet.channels; channel++) {
    const output = audioBuffer.getChannelData(channel);
    for (let frame = 0; frame < frames; frame++) {
      output[frame] = pcm.getInt16((frame * packet.channels + channel) * 2, true) / 32768;
    }
  }
  const now = context.currentTime;
  if (audioNextTime > now + audioMaximumLeadSeconds) stopScheduledAudio();
  if (audioNextTime < now + 0.015) audioNextTime = now + audioTargetLeadSeconds;
  const source = context.createBufferSource();
  source.buffer = audioBuffer;
  source.connect(audioGain);
  scheduledAudioSources.add(source);
  source.onended = () => scheduledAudioSources.delete(source);
  source.start(audioNextTime);
  audioNextTime += audioBuffer.duration;
  audioPacketsScheduled += 1;
  audioLastError = '';
}
function connectAudioWs() {
  if (audioWs && (audioWs.readyState === WebSocket.OPEN || audioWs.readyState === WebSocket.CONNECTING)) return;
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  audioWs = new WebSocket(`${protocol}//${location.host}/audio`);
  audioWs.binaryType = 'arraybuffer';
  audioWs.onmessage = ev => {
    audioPacketsReceived += 1;
    try {
      scheduleAudioPacket(ev.data);
    } catch (err) {
      audioPacketsRejected += 1;
      audioLastError = err?.name || 'decode';
      updateAudioButton();
    }
  };
  audioWs.onclose = () => {
    audioWs = null;
    stopScheduledAudio();
    clearTimeout(audioWsReconnectTimer);
    audioWsReconnectTimer = setTimeout(connectAudioWs, 1500);
  };
  audioWs.onerror = () => audioWs?.close();
}
function stopPolling() {
  if (poller) { clearInterval(poller); poller = null; }
}
function stopFramePolling() {
  if (framePoller) { clearInterval(framePoller); framePoller = null; }
}
function setOperationStatus(message = '', error = false) {
  operationStatusEl.textContent = message;
  operationStatusEl.classList.toggle('error', error);
}
function feedbackClientSnapshot() {
  let gamepads = [];
  try {
    gamepads = typeof navigator.getGamepads === 'function' ? Array.from(navigator.getGamepads()).filter(Boolean) : [];
  } catch (_) {}
  return {
    captured_at: new Date().toISOString(),
    user_agent: navigator.userAgent,
    language: navigator.language,
    platform: navigator.platform || '',
    visibility_state: document.visibilityState,
    viewport: {
      width: window.innerWidth,
      height: window.innerHeight,
      device_pixel_ratio: window.devicePixelRatio,
    },
    canvas: {
      width: screen.width,
      height: screen.height,
      client_width: screen.clientWidth,
      client_height: screen.clientHeight,
    },
    frontend: {
      orientation: currentOrientation,
      websocket_state: ws?.readyState ?? null,
      websocket_last_message_age_ms: wsLastMessageAt ? Math.round(performance.now() - wsLastMessageAt) : null,
      audio_enabled: audioEnabled,
      audio_context_state: audioContext?.state || 'uninitialized',
      audio_packets_received: audioPacketsReceived,
      audio_packets_scheduled: audioPacketsScheduled,
      audio_packets_rejected: audioPacketsRejected,
      audio_last_error: audioLastError,
      pointer_active: pointerActive,
      active_keyboard_keys: activeKeyboardKeys.size,
      active_button_keys: buttonKeyStates.size,
      active_gamepad_inputs: activeGamepadInputs.size,
      key_bindings: {...keyBindings},
      gamepad_bindings: {...gamepadBindings},
    },
    gamepads: gamepads.map(gamepad => ({
      index: gamepad.index,
      connected: gamepad.connected,
      mapping: gamepad.mapping,
      buttons: gamepad.buttons.length,
      axes: gamepad.axes.length,
    })),
  };
}
function feedbackDownloadName(response) {
  const disposition = response.headers.get('Content-Disposition') || '';
  const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  if (!encoded) return `bbk9588-feedback-${Date.now()}.zip`;
  try { return decodeURIComponent(encoded); } catch (_) { return encoded; }
}
async function saveFeedbackBundle() {
  if (feedbackRequestPending) return;
  feedbackRequestPending = true;
  saveFeedbackEl.disabled = true;
  setOperationStatus('正在收集反馈日志');
  try {
    const response = await fetch('/api/feedback', jsonRequest({client:feedbackClientSnapshot()}));
    if (!response.ok) {
      const body = await response.text();
      throw new Error(body || `HTTP ${response.status}`);
    }
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement('a');
    link.href = url;
    link.download = feedbackDownloadName(response);
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    setOperationStatus('反馈包已保存，请将 ZIP 文件附在问题反馈中');
  } catch (err) {
    console.error(err);
    setOperationStatus(`保存反馈包失败：${err?.message || String(err)}`, true);
  } finally {
    feedbackRequestPending = false;
    saveFeedbackEl.disabled = false;
  }
}
async function runLifecycleCommand(op, pendingLabel) {
  if (lifecycleRequestPending) return;
  lifecycleRequestPending = true;
  setOperationStatus(pendingLabel);
  stopEl.disabled = true;
  resetEl.disabled = true;
  forceStopEl.disabled = true;
  powerKeyEl.disabled = true;
  try {
    const status = await api('/api/command', jsonRequest({op}));
    renderStatus(status);
    setOperationStatus(status.lifecycle_notice || '');
  } catch (err) {
    console.error(err);
    setOperationStatus(err?.message || String(err), true);
  } finally {
    lifecycleRequestPending = false;
    ensurePolling();
    try { renderStatus(await api('/api/status')); } catch (err) { console.error(err); }
  }
}
function requestStop() { return runLifecycleCommand('stop', '正在等待固件安全关机'); }
async function restartEmulator() {
  stopScheduledAudio();
  return runLifecycleCommand('reset', '正在安全关机并重新启动');
}
resetEl.onclick = restartEmulator;
restartFromOverlayEl.onclick = restartEmulator;
stopEl.onclick = requestStop;
forceStopEl.onclick = () => {
  if (!window.confirm('强制停止可能中断 NAND 写入并损坏文件系统，确定继续？')) return;
  runLifecycleCommand('force-stop', '正在强制停止');
};
saveFeedbackEl.onclick = saveFeedbackBundle;
frontendInputCalibrationEl.onchange = () => {
  wsSend({op:'frontend-input-calibration', enabled:frontendInputCalibrationEl.checked});
};
usbPowerConnectedEl.onchange = () => {
  wsSend({op:'set-usb-power', connected:usbPowerConnectedEl.checked});
};
openSettingsEl.onclick = openSettingsDialog;
closeSettingsEl.onclick = closeSettingsDialog;
keymapSettingsTabEl.onclick = () => setSettingsTab('keymap');
touchSettingsTabEl.onclick = () => setSettingsTab('touch');
powerSettingsTabEl.onclick = () => setSettingsTab('power');
settingsDialogEl.addEventListener('cancel', ev => {
  ev.preventDefault();
  closeSettingsDialog();
});
settingsDialogEl.addEventListener('click', ev => {
  if (ev.target === settingsDialogEl) closeSettingsDialog();
});
openControlsDrawerEl.onclick = () => openDrawer('controls');
openStatusDrawerEl.onclick = () => openDrawer('status');
drawerBackdropEl.onclick = closeDrawers;
document.querySelectorAll('[data-close-drawer]').forEach(button => {
  button.addEventListener('click', closeDrawers);
});
const handleMobileLayoutChange = () => {
  if (!mobileLayoutQuery.matches) activeDrawer = null;
  syncDrawerState();
};
if (typeof mobileLayoutQuery.addEventListener === 'function') {
  mobileLayoutQuery.addEventListener('change', handleMobileLayoutChange);
} else {
  mobileLayoutQuery.addListener(handleMobileLayoutChange);
}
syncDrawerState();
statusTabButtonEl.onclick = () => setSidebarTab('status');
filesTabButtonEl.onclick = () => setSidebarTab('files');
document.getElementById('fileUp').onclick = () => {
  loadNandFiles(parentNandDirectory(currentNandDirectory)).catch(showFileManagerError);
};
document.getElementById('fileRefresh').onclick = () => {
  loadNandFiles(currentNandDirectory).catch(showFileManagerError);
};
document.getElementById('fileMkdir').onclick = () => {
  const name = window.prompt('文件夹名称');
  if (!name) return;
  runNandFileMutation(
    '/api/files/mkdir',
    jsonRequest({path:currentNandDirectory, name}),
    '正在新建文件夹'
  );
};
document.getElementById('fileImport').onclick = () => {
  if (nandFilesBusy) return;
  fileImportInputEl.value = '';
  fileImportInputEl.click();
};
fileImportInputEl.onchange = () => {
  const file = fileImportInputEl.files?.[0];
  if (!file) return;
  runNandFileMutation(
    `/api/files/import?path=${encodeURIComponent(currentNandDirectory)}&name=${encodeURIComponent(file.name)}`,
    {method:'POST', headers:{'Content-Type':'application/octet-stream'}, body:file},
    '正在导入'
  );
};
rotateLeftEl.onclick = () => requestRotation(-1);
rotateRightEl.onclick = () => requestRotation(1);
toggleAudioEl.onclick = () => {
  const running = audioContext?.state === 'running' && !audioClockStalled;
  if (!audioEnabled) {
    audioEnabled = true;
    unlockAudio();
  } else if (!running) {
    unlockAudio();
  } else {
    audioEnabled = false;
    stopScheduledAudio();
  }
  updateAudioButton();
};
toggleFullscreenEl.onclick = toggleFullscreen;
exitFullscreenEl.onclick = toggleFullscreen;
toggleFullscreenEl.disabled = !(
  document.fullscreenEnabled || document.webkitFullscreenEnabled ||
  screenWrapEl.requestFullscreen || screenWrapEl.webkitRequestFullscreen
);
document.addEventListener('fullscreenchange', updateFullscreenScreenSize);
document.addEventListener('webkitfullscreenchange', updateFullscreenScreenSize);
window.addEventListener('resize', updateFullscreenScreenSize);
if ('ResizeObserver' in window) {
  const screenResizeObserver = new ResizeObserver(updateFullscreenScreenSize);
  screenResizeObserver.observe(screenWrapEl);
}

function loadKeyBindings() {
  const bindings = {...defaultKeyBindings};
  try {
    const saved = JSON.parse(localStorage.getItem(keyBindingStorageKey) || '{}');
    for (const code of Object.keys(bindings)) {
      if (typeof saved[code] === 'string' && saved[code]) bindings[code] = saved[code];
    }
    let migrated = false;
    for (const [code, legacyBinding] of Object.entries(legacyDefaultKeyBindings)) {
      const replacement = defaultKeyBindings[code];
      const replacementInUse = Object.entries(bindings).some(
        ([otherCode, binding]) => otherCode !== code && binding === replacement
      );
      if (bindings[code] === legacyBinding && !replacementInUse) {
        bindings[code] = replacement;
        saved[code] = replacement;
        migrated = true;
      }
    }
    if (new Set(Object.values(bindings)).size !== Object.keys(bindings).length) {
      return {...defaultKeyBindings};
    }
    if (migrated) localStorage.setItem(keyBindingStorageKey, JSON.stringify(saved));
  } catch (err) {
    console.error(err);
  }
  return bindings;
}
function loadGamepadBindings() {
  const bindings = {...defaultGamepadBindings};
  try {
    const saved = JSON.parse(localStorage.getItem(gamepadBindingStorageKey) || '{}');
    for (const code of Object.keys(bindings)) {
      if (typeof saved[code] === 'string' && saved[code]) bindings[code] = saved[code];
    }
    let migrated = false;
    for (const [code, legacyBinding] of Object.entries(legacyDefaultGamepadBindings)) {
      const replacement = defaultGamepadBindings[code];
      const replacementInUse = Object.entries(bindings).some(
        ([otherCode, binding]) => otherCode !== code && binding === replacement
      );
      if (bindings[code] === legacyBinding && !replacementInUse) {
        bindings[code] = replacement;
        saved[code] = replacement;
        migrated = true;
      }
    }
    if (new Set(Object.values(bindings)).size !== Object.keys(bindings).length) {
      return {...defaultGamepadBindings};
    }
    if (migrated) localStorage.setItem(gamepadBindingStorageKey, JSON.stringify(saved));
  } catch (err) {
    console.error(err);
  }
  return bindings;
}
function saveKeyBindings() {
  try {
    localStorage.setItem(keyBindingStorageKey, JSON.stringify(keyBindings));
    localStorage.setItem(gamepadBindingStorageKey, JSON.stringify(gamepadBindings));
  } catch (err) {
    console.error(err);
  }
}
function keyboardCodeLabel(code) {
  if (code === 'Space') return 'Space';
  if (code === 'Escape') return 'Esc';
  if (code === 'Enter') return 'Enter';
  if (code === 'Backspace') return 'Backspace';
  if (code.startsWith('Key')) return code.slice(3);
  if (code.startsWith('Digit')) return code.slice(5);
  if (code.startsWith('Numpad')) return `Num ${code.slice(6)}`;
  if (code.startsWith('Arrow')) return code.slice(5);
  return code;
}
function gamepadBindingLabel(binding) {
  const button = /^button:(\d+)$/.exec(binding || '');
  if (button) {
    const index = Number(button[1]);
    const dpadLabels = {12:'手柄↑', 13:'手柄↓', 14:'手柄←', 15:'手柄→'};
    return dpadLabels[index] || `手柄 B${index}`;
  }
  const axis = /^axis:(\d+):([+-])$/.exec(binding || '');
  if (axis) {
    const leftStickLabels = {
      '0:-':'左摇杆←',
      '0:+':'左摇杆→',
      '1:-':'左摇杆↑',
      '1:+':'左摇杆↓',
    };
    return leftStickLabels[`${axis[1]}:${axis[2]}`] || `手柄轴${axis[1]}${axis[2]}`;
  }
  return binding || '未设置';
}
function updateKeyBindingUi() {
  document.querySelectorAll('[data-key-hint]').forEach(el => {
    el.textContent = keyboardCodeLabel(keyBindings[String(el.dataset.keyHint)] || '');
  });
  document.querySelectorAll('[data-binding-code]').forEach(btn => {
    const code = String(btn.dataset.bindingCode);
    const capturing = bindingCaptureCode === code;
    btn.classList.toggle('capturing', capturing);
    const keyboardLabel = keyboardCodeLabel(keyBindings[code] || '');
    const gamepadLabel = gamepadBindingLabel(gamepadBindings[code] || '');
    btn.textContent = capturing ? '输入…' : `${keyboardLabel} / ${gamepadLabel}`;
    btn.title = capturing
      ? '按键盘、手柄按钮或推动摇杆'
      : `键盘：${keyboardLabel}；手柄：${gamepadLabel}`;
  });
}
function beginBindingCapture(code) {
  gamepadInputFocused = true;
  bindingCaptureCode = String(code);
  updateGamepadStatus('等待输入：请按手柄按钮或推动摇杆');
  updateKeyBindingUi();
}
function assignCapturedBinding(bindings, physicalCode) {
  const targetCode = bindingCaptureCode;
  if (targetCode === null) return false;
  const previous = bindings[targetCode];
  const duplicate = Object.keys(bindings).find(code => code !== targetCode && bindings[code] === physicalCode);
  if (duplicate) bindings[duplicate] = previous;
  bindings[targetCode] = physicalCode;
  bindingCaptureCode = null;
  saveKeyBindings();
  updateKeyBindingUi();
  if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
  return true;
}
document.querySelectorAll('[data-binding-code]').forEach(btn => {
  btn.addEventListener('click', ev => {
    ev.preventDefault();
    beginBindingCapture(btn.dataset.bindingCode);
  });
});
resetKeyBindingsEl.onclick = () => {
  keyBindings = {...defaultKeyBindings};
  gamepadBindings = {...defaultGamepadBindings};
  bindingCaptureCode = null;
  saveKeyBindings();
  updateKeyBindingUi();
};
const activeButtonPointers = new Map();
const buttonKeyStates = new Map();
const activeKeyboardKeys = new Map();
const activeGamepadInputs = new Map();
const gamepadPreviousStates = new Map();
const captureSuppressedGamepadInputs = new Set();
const gamepadPressThreshold = 0.55;
const gamepadReleaseThreshold = 0.35;
const gamepadCaptureThreshold = 0.5;
let gamepadInputFocused = document.hasFocus();
let gamepadStatusText = '';
function updateGamepadStatus(text, error = false) {
  if (gamepadStatusText === text && gamepadStatusEl.classList.contains('error') === error) return;
  gamepadStatusText = text;
  gamepadStatusEl.textContent = text;
  gamepadStatusEl.classList.toggle('error', error);
}
function updateDeviceKeyActive(code) {
  const btn = document.querySelector(`[data-key="${code}"]`);
  if (!btn) return;
  const keyboardActive = Array.from(activeKeyboardKeys.values()).includes(code);
  const gamepadActive = Array.from(activeGamepadInputs.values()).some(state => state.code === code);
  btn.classList.toggle('active', buttonKeyStates.has(code) || keyboardActive || gamepadActive);
}
function sendKeyButton(btn, down, phase = '') {
  btn.classList.toggle('active', down);
  wsSend({
    op:'key',
    code:Number(btn.dataset.key),
    name:btn.dataset.name || '',
    down,
    phase,
    source:'button',
    input_session:inputSessionId,
    advance:false,
    reply:false,
  });
}
function beginKeyButton(btn) {
  const code = Number(btn.dataset.key);
  const pending = buttonKeyStates.get(code);
  if (pending?.releaseTimer) {
    clearTimeout(pending.releaseTimer);
    pending.releaseTimer = null;
    btn.classList.add('active');
    return;
  }
  if (pending) return;
  sendKeyButton(btn, true, 'down');
  buttonKeyStates.set(code, {btn, downAt:performance.now(), releaseTimer:null});
}
function endKeyButton(btn, phase) {
  const code = Number(btn.dataset.key);
  const state = buttonKeyStates.get(code);
  if (!state || state.releaseTimer) return;
  const delay = Math.max(0, minKeyHoldMs - (performance.now() - state.downAt));
  state.releaseTimer = setTimeout(() => {
    sendKeyButton(state.btn, false, phase);
    buttonKeyStates.delete(code);
  }, delay);
}
document.querySelectorAll('[data-key]').forEach(btn => {
  btn.addEventListener('contextmenu', ev => ev.preventDefault());
  btn.addEventListener('selectstart', ev => ev.preventDefault());
  btn.addEventListener('dblclick', ev => ev.preventDefault());
  btn.addEventListener('pointerdown', ev => {
    ev.preventDefault();
    if (activeButtonPointers.has(ev.pointerId)) return;
    activeButtonPointers.set(ev.pointerId, btn);
    btn.setPointerCapture?.(ev.pointerId);
    beginKeyButton(btn);
  });
  btn.addEventListener('pointerup', ev => {
    ev.preventDefault();
    const active = activeButtonPointers.get(ev.pointerId);
    if (!active) return;
    activeButtonPointers.delete(ev.pointerId);
    active.releasePointerCapture?.(ev.pointerId);
    endKeyButton(active, 'up');
  });
  btn.addEventListener('pointercancel', ev => {
    const active = activeButtonPointers.get(ev.pointerId);
    if (!active) return;
    activeButtonPointers.delete(ev.pointerId);
    endKeyButton(active, 'cancel');
  });
});
function keyCodeFromKeyboard(ev) {
  for (const [guestCode, physicalCode] of Object.entries(keyBindings)) {
    if (physicalCode === ev.code) return Number(guestCode);
  }
  return null;
}
function isEditableTarget(target) {
  if (!(target instanceof HTMLElement)) return false;
  return target.isContentEditable || ['INPUT', 'SELECT', 'TEXTAREA'].includes(target.tagName);
}
window.addEventListener('keydown', ev => {
  if (ev.code === 'Escape' && activeDrawer !== null) {
    ev.preventDefault();
    closeDrawers();
    return;
  }
  if (bindingCaptureCode !== null) {
    ev.preventDefault();
    ev.stopPropagation();
    assignCapturedBinding(keyBindings, ev.code);
    return;
  }
  if (settingsDialogEl.open) return;
  if (ev.code === 'Escape' && currentFullscreenElement() === screenWrapEl) return;
  if (isEditableTarget(ev.target)) return;
  const code = keyCodeFromKeyboard(ev);
  if (code === null || activeKeyboardKeys.has(ev.code)) return;
  ev.preventDefault();
  activeKeyboardKeys.set(ev.code, code);
  updateDeviceKeyActive(code);
  wsSend({op:'key', code, down:true, source:'keyboard', input_session:inputSessionId, advance:false, reply:false});
});
window.addEventListener('keyup', ev => {
  const code = activeKeyboardKeys.get(ev.code);
  if (code === undefined) return;
  ev.preventDefault();
  activeKeyboardKeys.delete(ev.code);
  wsSend({op:'key', code, down:false, source:'keyboard', input_session:inputSessionId, advance:false, reply:false});
  updateDeviceKeyActive(code);
});
function releaseKeyboardInputs(source = 'keyboard-blur') {
  const releasedCodes = new Set(activeKeyboardKeys.values());
  for (const code of activeKeyboardKeys.values()) {
    wsSend({op:'key', code, down:false, source, input_session:inputSessionId, advance:false, reply:false});
  }
  activeKeyboardKeys.clear();
  releasedCodes.forEach(updateDeviceKeyActive);
}
function releaseButtonInputs(phase = 'button-blur') {
  for (const [code, state] of Array.from(buttonKeyStates.entries())) {
    if (state.releaseTimer) clearTimeout(state.releaseTimer);
    sendKeyButton(state.btn, false, phase);
    buttonKeyStates.delete(code);
  }
  activeButtonPointers.clear();
}
function gamepadSnapshot(gamepad) {
  return {
    buttons:Array.from(gamepad.buttons, button => Boolean(button.pressed || button.value >= 0.5)),
    axes:Array.from(gamepad.axes, value => Number(value) || 0),
  };
}
function gamepadHasActivity(gamepad) {
  return gamepad.buttons.some(button => button.pressed || button.value >= 0.5) ||
    gamepad.axes.some(value => Math.abs(Number(value) || 0) >= gamepadPressThreshold);
}
function capturedGamepadBinding(gamepad, previous) {
  for (let index = 0; index < gamepad.buttons.length; index += 1) {
    const active = Boolean(gamepad.buttons[index].pressed || gamepad.buttons[index].value >= 0.5);
    if (active && !previous?.buttons?.[index]) return `button:${index}`;
  }
  for (let index = 0; index < gamepad.axes.length; index += 1) {
    const value = Number(gamepad.axes[index]) || 0;
    const previousValue = Number(previous?.axes?.[index]) || 0;
    if (Math.abs(value) >= gamepadCaptureThreshold && Math.abs(previousValue) < gamepadCaptureThreshold) {
      return `axis:${index}:${value < 0 ? '-' : '+'}`;
    }
  }
  return null;
}
function gamepadBindingActive(gamepad, binding, wasActive) {
  const button = /^button:(\d+)$/.exec(binding || '');
  if (button) {
    const state = gamepad.buttons[Number(button[1])];
    return Boolean(state && (state.pressed || state.value >= 0.5));
  }
  const axis = /^axis:(\d+):([+-])$/.exec(binding || '');
  if (!axis) return false;
  const value = Number(gamepad.axes[Number(axis[1])]) || 0;
  const threshold = wasActive ? gamepadReleaseThreshold : gamepadPressThreshold;
  return axis[2] === '+' ? value >= threshold : value <= -threshold;
}
function sendGamepadKey(code, down, phase) {
  wsSend({op:'key', code, down, source:'gamepad', phase, input_session:inputSessionId, advance:false, reply:false});
}
function beginGamepadInput(sourceId, code) {
  const existing = activeGamepadInputs.get(sourceId);
  if (existing?.releaseTimer) {
    clearTimeout(existing.releaseTimer);
    existing.releaseTimer = null;
    updateDeviceKeyActive(code);
    return;
  }
  if (existing) return;
  const alreadyDown = Array.from(activeGamepadInputs.values()).some(state => state.code === code);
  activeGamepadInputs.set(sourceId, {code, downAt:performance.now(), releaseTimer:null});
  if (!alreadyDown) sendGamepadKey(code, true, 'down');
  updateDeviceKeyActive(code);
}
function endGamepadInput(sourceId, phase = 'up', immediate = false) {
  const state = activeGamepadInputs.get(sourceId);
  if (!state || state.releaseTimer) return;
  const release = () => {
    activeGamepadInputs.delete(sourceId);
    const stillDown = Array.from(activeGamepadInputs.values()).some(other => other.code === state.code);
    if (!stillDown) sendGamepadKey(state.code, false, phase);
    updateDeviceKeyActive(state.code);
  };
  if (immediate) {
    release();
    return;
  }
  const delay = Math.max(0, minKeyHoldMs - (performance.now() - state.downAt));
  state.releaseTimer = setTimeout(release, delay);
}
function releaseGamepadInputs(phase = 'disconnect') {
  for (const sourceId of Array.from(activeGamepadInputs.keys())) {
    const state = activeGamepadInputs.get(sourceId);
    if (state?.releaseTimer) clearTimeout(state.releaseTimer);
    if (state) state.releaseTimer = null;
    endGamepadInput(sourceId, phase, true);
  }
}
function refreshActiveKeyLeases() {
  const activeCodes = new Set(activeKeyboardKeys.values());
  for (const code of buttonKeyStates.keys()) activeCodes.add(code);
  for (const state of activeGamepadInputs.values()) activeCodes.add(state.code);
  for (const code of activeCodes) {
    wsSend({
      op:'key',
      code,
      down:true,
      source:'heartbeat',
      phase:'heartbeat',
      input_session:inputSessionId,
      advance:false,
      reply:false,
    });
  }
}
setInterval(refreshActiveKeyLeases, keyLeaseHeartbeatMs);
function readGamepads() {
  if (typeof navigator.getGamepads !== 'function') {
    updateGamepadStatus('当前浏览器未提供 Gamepad API', true);
    return [];
  }
  try {
    return Array.from(navigator.getGamepads()).filter(Boolean);
  } catch (err) {
    updateGamepadStatus(`Gamepad API 不可用：${err?.message || err}`, true);
    return [];
  }
}
function pollGamepads() {
  const visible = document.visibilityState === 'visible';
  const detectedGamepads = visible ? readGamepads() : [];
  if (detectedGamepads.some(gamepadHasActivity)) gamepadInputFocused = true;
  const gamepads = gamepadInputFocused ? detectedGamepads : [];
  const connected = new Set();
  const seenSources = new Set();
  if (visible && detectedGamepads.length === 0 && bindingCaptureCode !== null) {
    updateGamepadStatus('未检测到手柄；请先按一次手柄按键');
  } else if (detectedGamepads.length > 0) {
    const name = String(detectedGamepads[0].id || `手柄 ${detectedGamepads[0].index + 1}`);
    updateGamepadStatus(`已连接：${name}`);
  }
  for (const gamepad of gamepads) {
    connected.add(gamepad.index);
    const previous = gamepadPreviousStates.get(gamepad.index);
    if (bindingCaptureCode !== null) {
      const captured = capturedGamepadBinding(gamepad, previous);
      if (captured) {
        captureSuppressedGamepadInputs.add(`${gamepad.index}:${captured}`);
        assignCapturedBinding(gamepadBindings, captured);
        updateGamepadStatus(`已映射：${gamepadBindingLabel(captured)}`);
      }
    }
    if (settingsDialogEl.open) {
      gamepadPreviousStates.set(gamepad.index, gamepadSnapshot(gamepad));
      continue;
    }
    for (const [guestCodeText, binding] of Object.entries(gamepadBindings)) {
      const code = Number(guestCodeText);
      const sourceId = `${gamepad.index}:${code}:${binding}`;
      seenSources.add(sourceId);
      const active = gamepadBindingActive(gamepad, binding, activeGamepadInputs.has(sourceId));
      const suppressionId = `${gamepad.index}:${binding}`;
      if (captureSuppressedGamepadInputs.has(suppressionId)) {
        if (!active) captureSuppressedGamepadInputs.delete(suppressionId);
        endGamepadInput(sourceId, 'capture', true);
        continue;
      }
      if (active) beginGamepadInput(sourceId, code);
      else endGamepadInput(sourceId);
    }
    gamepadPreviousStates.set(gamepad.index, gamepadSnapshot(gamepad));
  }
  for (const sourceId of Array.from(activeGamepadInputs.keys())) {
    if (!seenSources.has(sourceId)) endGamepadInput(sourceId, visible ? 'disconnect' : 'hidden', true);
  }
  for (const index of Array.from(gamepadPreviousStates.keys())) {
    if (!connected.has(index)) {
      gamepadPreviousStates.delete(index);
      for (const suppressionId of Array.from(captureSuppressedGamepadInputs)) {
        if (suppressionId.startsWith(`${index}:`)) captureSuppressedGamepadInputs.delete(suppressionId);
      }
    }
  }
  requestAnimationFrame(pollGamepads);
}
window.addEventListener('blur', () => {
  gamepadInputFocused = false;
  releaseButtonInputs();
  releaseKeyboardInputs();
  releaseGamepadInputs('gamepad-blur');
});
window.addEventListener('focus', () => { gamepadInputFocused = true; });
window.addEventListener('pointerdown', () => {
  gamepadInputFocused = true;
  unlockAudio();
}, {capture:true});
window.addEventListener('touchend', unlockAudio, {capture:true, passive:true});
window.addEventListener('keydown', unlockAudio, {capture:true});
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState !== 'visible') {
    releaseGamepadInputs('gamepad-hidden');
    stopScheduledAudio();
    audioContext?.suspend().catch(() => {});
  } else if (audioEnabled && audioContext) {
    audioContext.resume().catch(() => {});
  }
});
window.addEventListener('gamepadconnected', ev => {
  gamepadInputFocused = true;
  updateGamepadStatus(`已连接：${ev.gamepad.id || `手柄 ${ev.gamepad.index + 1}`}`);
});
window.addEventListener('gamepaddisconnected', () => {
  releaseGamepadInputs('gamepad-disconnect');
  updateGamepadStatus('手柄已断开；请按任意手柄键重新连接');
});
window.addEventListener('pagehide', () => {
  releaseButtonInputs('button-pagehide');
  releaseKeyboardInputs('keyboard-pagehide');
  releaseGamepadInputs('gamepad-pagehide');
});
requestAnimationFrame(pollGamepads);
screen.addEventListener('pointerdown', ev => {
  ev.preventDefault();
  ev.stopPropagation();
  cancelPendingTouchRelease();
  clearPendingTouchMove();
  touchMoveAwaitingFrame = false;
  if (sendTouchAt(ev.clientX, ev.clientY, true, 'down', ev.pointerType || 'pointer')) {
    pointerActive = true;
    activePointerId = ev.pointerId;
    touchDownAt = performance.now();
    screen.setPointerCapture?.(ev.pointerId);
  }
});
screen.addEventListener('pointermove', ev => {
  if (!pointerActive || ev.pointerId !== activePointerId) return;
  ev.preventDefault();
  ev.stopPropagation();
  cancelPendingTouchRelease();
  queueTouchMove(ev.clientX, ev.clientY, ev.pointerType || 'pointer');
});
screen.addEventListener('pointerup', ev => {
  if (!pointerActive || ev.pointerId !== activePointerId) return;
  ev.preventDefault();
  ev.stopPropagation();
  sendTouchReleaseAt(ev.clientX, ev.clientY, 'up', ev.pointerType || 'pointer', true);
  pointerActive = false;
  activePointerId = null;
  screen.releasePointerCapture?.(ev.pointerId);
});
screen.addEventListener('pointercancel', ev => {
  if (!pointerActive || ev.pointerId !== activePointerId) return;
  ev.preventDefault();
  ev.stopPropagation();
  sendTouchReleaseAt(ev.clientX, ev.clientY, 'cancel', ev.pointerType || 'pointer', true);
  pointerActive = false;
  activePointerId = null;
});
screen.addEventListener('mousedown', ev => {
  if (window.PointerEvent) return;
  ev.preventDefault();
  ev.stopPropagation();
  cancelPendingTouchRelease();
  clearPendingTouchMove();
  touchMoveAwaitingFrame = false;
  if (sendTouchAt(ev.clientX, ev.clientY, true, 'down', 'mouse')) {
    pointerActive = true;
    touchDownAt = performance.now();
  }
});
screen.addEventListener('mousemove', ev => {
  if (window.PointerEvent || !pointerActive) return;
  ev.preventDefault();
  ev.stopPropagation();
  cancelPendingTouchRelease();
  queueTouchMove(ev.clientX, ev.clientY, 'mouse');
});
window.addEventListener('mouseup', ev => {
  if (window.PointerEvent || !pointerActive) return;
  sendTouchReleaseAt(ev.clientX, ev.clientY, 'up', 'mouse', true);
  pointerActive = false;
});
screen.addEventListener('touchstart', ev => {
  if (window.PointerEvent) return;
  ev.preventDefault();
  const t = ev.changedTouches[0];
  cancelPendingTouchRelease();
  clearPendingTouchMove();
  touchMoveAwaitingFrame = false;
  if (t && sendTouchAt(t.clientX, t.clientY, true, 'down', 'touch')) {
    pointerActive = true;
    touchDownAt = performance.now();
  }
}, {passive:false});
screen.addEventListener('touchmove', ev => {
  if (window.PointerEvent || !pointerActive) return;
  ev.preventDefault();
  const t = ev.changedTouches[0];
  if (t) {
    cancelPendingTouchRelease();
    queueTouchMove(t.clientX, t.clientY, 'touch');
  }
}, {passive:false});
screen.addEventListener('touchend', ev => {
  if (window.PointerEvent || !pointerActive) return;
  ev.preventDefault();
  const t = ev.changedTouches[0];
  if (t) sendTouchReleaseAt(t.clientX, t.clientY, 'up', 'touch', true);
  pointerActive = false;
}, {passive:false});
updateOrientationControls();
updateKeyBindingUi();
updateAudioButton();
connectWs();
connectAudioWs();
refresh().catch(console.error);
</script>
</body>
</html>
"""

Handler.html = HTML


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Serve a local BBK 9588 QEMU frontend.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--ram-mb", type=int, default=160)
    ap.add_argument(
        "--frame-push-min-interval",
        type=float,
        default=1.0 / 30.0,
        help="Minimum seconds between QEMU frame-chardev WebSocket frame pushes.",
    )
    ap.add_argument(
        "--frame-info-min-interval",
        type=float,
        default=1.0,
        help="Minimum seconds between full framebuffer-stat rescans for status JSON.",
    )
    ap.add_argument("--boot-mode", choices=["nand", "c200", "uboot"], default="nand", help="QEMU cold-boot path.")
    ap.add_argument(
        "--image",
        type=Path,
        help="Optional direct boot image path for c200/uboot compatibility modes.",
    )
    ap.add_argument(
        "--payload",
        type=Path,
        help="Optional legacy C200 RAM preload for uboot mode.",
    )
    ap.add_argument("--nand-image", type=Path, help="Raw NAND image backing the frontend emulator.")
    ap.add_argument(
        "--nand-import-source",
        type=Path,
        help="Validated .bin or single-image ZIP to atomically import before starting QEMU.",
    )
    ap.add_argument(
        "--no-nand",
        action="store_true",
        help="Do not attach NAND in direct-boot diagnostic runs.",
    )
    ap.add_argument(
        "--frontend-input-calibration",
        dest="frontend_input_calibration",
        action="store_true",
        default=False,
        help="Frontend diagnostic helper: feed cold-boot calibration touches through the QEMU input chardev.",
    )
    ap.add_argument(
        "--no-frontend-input-calibration",
        dest="frontend_input_calibration",
        action="store_false",
        help="Disable the frontend input calibration helper.",
    )
    ap.add_argument("--orientation", choices=["raw", "rot180", "cw90", "ccw90", "hflip", "vflip"], default="rot180")
    ap.add_argument("--profile-out", type=Path, help="Write a cProfile report when the frontend exits normally.")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--backend", choices=["qemu"], default="qemu", help=argparse.SUPPRESS)
    ap.add_argument("--qemu", default=DEFAULT_QEMU_EXECUTABLE, help="QEMU executable.")
    ap.add_argument("--qemu-machine", default=DEFAULT_QEMU_MACHINE, help="QEMU machine.")
    ap.add_argument("--qemu-cpu", default=DEFAULT_QEMU_CPU, help="QEMU CPU model.")
    ap.add_argument("--qemu-accel", default="tcg,thread=multi,tb-size=256", help="QEMU accelerator options.")
    ap.add_argument(
        "--qemu-icount",
        default=DEFAULT_WEB_QEMU_ICOUNT,
        help="QEMU instruction clock options used to keep guest timers stable under load.",
    )
    ap.add_argument(
        "--no-qemu-icount",
        dest="qemu_icount",
        action="store_const",
        const=None,
        help="Disable the Web frontend's instruction-clock timing mode.",
    )
    ap.add_argument("--qemu-gdb", default="none", help="QEMU GDB stub target; use 'auto' to allocate a local port.")
    ap.add_argument("--qemu-timeout", type=float, default=5.0, help="Default bounded-run timeout used by QEMU probes.")
    ap.add_argument(
        "--qemu-host-audio",
        action="store_true",
        default=False,
        help="Also play QEMU audio on the server host; Web audio remains enabled.",
    )
    ap.add_argument(
        "--qemu-machine-option",
        action="append",
        default=[],
        help="Append one diagnostic bbk9588 -M option, for example progress-trace=on. Can be repeated.",
    )
    ap.add_argument("--qemu-extra-arg", action="append", default=[], help="Append one raw QEMU argument. Can be repeated.")
    ap.add_argument(
        "--qemu-firmware-patch",
        action="append",
        default=None,
        help="Legacy diagnostic QEMU-only firmware patch name for compatibility runs, or 'none'.",
    )
    ap.add_argument(
        "--allow-gdb-diagnostics",
        action="store_true",
        default=False,
        help="Enable explicit intrusive GDB diagnostics such as write watches and breakpoint traces.",
    )
    args = ap.parse_args(argv)
    args.backend = "qemu"

    state = FrontendState(args)
    Handler.state = state
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"BBK9588 HWEMU frontend: http://{args.host}:{args.port}/")
    profiler = cProfile.Profile() if args.profile_out is not None else None
    try:
        if profiler is None:
            httpd.serve_forever()
        else:
            profiler.enable()
            httpd.serve_forever()
            profiler.disable()
    except KeyboardInterrupt:
        if profiler is not None:
            profiler.disable()
    finally:
        try:
            state.close()
        except Exception:
            pass
        httpd.server_close()
        if profiler is not None and args.profile_out is not None:
            args.profile_out.parent.mkdir(parents=True, exist_ok=True)
            stats_path = args.profile_out
            profiler.dump_stats(str(stats_path))
            text_path = stats_path.with_suffix(stats_path.suffix + ".txt")
            with text_path.open("w", encoding="utf-8") as fh:
                stats = pstats.Stats(profiler, stream=fh).strip_dirs().sort_stats("cumtime")
                stats.print_stats(80)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
