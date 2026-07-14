// swift-tools-version: 6.0

import PackageDescription

let package = Package(
    name: "KaideraOSOperator",
    platforms: [
        .macOS(.v13)
    ],
    products: [
        .executable(name: "KaideraOSOperator", targets: ["KaideraOSOperator"])
    ],
    targets: [
        .target(name: "KaideraOSOperatorCore"),
        .executableTarget(
            name: "KaideraOSOperator",
            dependencies: ["KaideraOSOperatorCore"],
            resources: [
                .process("Resources")
            ]
        ),
        .testTarget(
            name: "KaideraOSOperatorCoreTests",
            dependencies: ["KaideraOSOperatorCore"]
        )
    ]
)
