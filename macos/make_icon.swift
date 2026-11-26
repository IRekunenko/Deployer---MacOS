// Draws the Deployer app icon into an .iconset folder (used by build_app.sh).
import AppKit

let out = CommandLine.arguments[1]
for s in [16, 32, 64, 128, 256, 512, 1024] {
    let img = NSImage(size: NSSize(width: s, height: s))
    img.lockFocus()
    let r = NSRect(x: 0, y: 0, width: s, height: s).insetBy(dx: CGFloat(s) * 0.09, dy: CGFloat(s) * 0.09)
    let path = NSBezierPath(roundedRect: r, xRadius: CGFloat(s) * 0.18, yRadius: CGFloat(s) * 0.18)
    NSGradient(starting: NSColor(calibratedRed: 0.15, green: 0.39, blue: 0.92, alpha: 1),
               ending: NSColor(calibratedRed: 0.05, green: 0.6, blue: 0.45, alpha: 1))!.draw(in: path, angle: -60)
    let attrs: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: CGFloat(s) * 0.5)]
    let str = NSAttributedString(string: "⚡", attributes: attrs)
    let sz = str.size()
    str.draw(at: NSPoint(x: (CGFloat(s) - sz.width) / 2, y: (CGFloat(s) - sz.height) / 2))
    img.unlockFocus()
    let rep = NSBitmapImageRep(data: img.tiffRepresentation!)!
    let png = rep.representation(using: .png, properties: [:])!
    func save(_ n: String) { try! png.write(to: URL(fileURLWithPath: out + "/" + n)) }
    switch s {
    case 16: save("icon_16x16.png")
    case 32: save("icon_16x16@2x.png"); save("icon_32x32.png")
    case 64: save("icon_32x32@2x.png")
    case 128: save("icon_128x128.png")
    case 256: save("icon_128x128@2x.png"); save("icon_256x256.png")
    case 512: save("icon_256x256@2x.png"); save("icon_512x512.png")
    default: save("icon_512x512@2x.png")
    }
}
